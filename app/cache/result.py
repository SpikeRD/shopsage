"""**结果缓存**：带 TTL 的普通键值缓存（`13.cache.md` §2 的 `result_cache_get/set`）。

## 它与语义缓存的区别（一张表说清）

| | 语义缓存（`semantic.py`） | 结果缓存（本模块） |
|---|---|---|
| 键 | 问题的**向量**（相似即命中） | 调用方给的**字符串**（完全匹配才命中） |
| 值 | 答案 + 出处 + 时间戳（要溯源） | 任意 JSON |
| 命中率来源 | 「同义改写」（怎么退货 / 退货流程是啥） | 「同一个键被反复请求」 |
| 判错的后果 | **答错问题**（最严重） | 拿到旧结果（调用方自己知道键意味着什么） |
| 谁用 | RAG 问答链路 | 报表、聚合、外部只读查询 |

**为什么两者都要有**：它们的正确性来源不同。语义缓存的正确性靠**阈值 + 溯源**
（所以它必须存出处、必须保守）；结果缓存的正确性靠**调用方给的键本身足够精确**
（`report:user=2:month=2026-09` 这个键命中的一定就是那份报表）。
把结果缓存做成语义的，等于让「本月报表」和「上月报表」有可能互相命中 ——
而那是一个没人能解释的错误。

## 三条刻意的口径

1. **键由调用方派生，不由本模块猜**。`result_cache_set(kind="report", key="u2:2026-09", ...)`
   —— `kind` 只进 Redis key 的命名空间（用于 `--purge` 按类清）。
   「键怎么算」是业务知识，缓存层不该替它决定（与第 12 步「幂等键由调用方派生」同口径）。
2. **序列化失败立刻报错**，不静默跳过：值里出现不可 JSON 化的对象（一个 `datetime`、
   一个 ORM 实例）是**调用方的 bug**，静默变成一个永不命中的缓存会让它查很久。
3. **TTL 必须 > 0**：`result_cache_set(ttl_seconds=0)` 抛 `ValueError`。
   写「永久缓存」需要一个显式的极大值 —— 让「忘了写 TTL」不可能静默变成永久缓存。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.cache.backend import KeyValue
from app.cache.settings import CacheSettings

logger = logging.getLogger(__name__)

#: 结果缓存的键空间（与语义缓存的 `entry:` 并列，便于按类清理）
RESULT_PREFIX = "result"


class ResultCacheError(ValueError):
    """结果缓存的入参错误（键非法 / TTL 非法 / 值不可序列化）：消息必须能照着修。"""


@dataclass(frozen=True)
class ResultHit:
    """一次结果缓存命中（`value` + 它躺了多久）。"""

    value: Any
    age_seconds: float
    kind: str
    key: str

    def summary(self) -> str:
        return f"命中 {self.kind}:{self.key}（躺了 {self.age_seconds:.1f}s）"


class ResultStore:
    """带 TTL 的键值缓存（Redis；不可用时由 `backend.build_key_value` 回落到内存）。

    值的形状：`{"value": ..., "created_at": 1730000000.0, "kind": "report"}` ——
    多包一层是为了让 `age_seconds` 可算（「这条为什么是旧的」要看它），
    而直接存裸值就只能靠 Redis 的 TTL 反推（`pttl` 拿到的是剩余时间，
    于是「刚写入」与「快过期」在账目上无法区分）。
    """

    def __init__(
        self,
        key_value: KeyValue,
        *,
        namespace: str = "cache",
        instance: str = "default",
        settings: CacheSettings | None = None,
    ) -> None:
        self.kv = key_value
        self.namespace = str(namespace)
        self.instance = str(instance)
        self.settings = settings or CacheSettings()
        self.hits = 0
        self.misses = 0
        self.writes = 0

    # -- key ---------------------------------------------------------------
    def key_for(self, kind: str, key: str) -> str:
        """结果缓存的 key（**唯一构造处**）。

        `kind` 与 `key` 都做字符白名单校验：Redis 的 key 里出现换行/空格在排障时
        几乎没法复制粘贴，而**非法键静默可用**是最难发现的一类问题
        （与第 12 步 `jobs.normalize_key`「抛错而不是静默清洗」同口径）。
        """
        for name, value in (("kind", kind), ("key", key)):
            text = str(value or "")
            if not text or len(text) > 200:
                raise ResultCacheError(f"结果缓存的 {name} 必须非空且不超过 200 字符，收到 {value!r}")
            if any(char in text for char in " \t\r\n"):
                raise ResultCacheError(
                    f"结果缓存的 {name} 不能含空白字符，收到 {value!r}"
                    "（白名单校验是刻意的：非法键静默可用会让「为什么缓存没命中」查很久）"
                )
        return f"{self.namespace}:{self.instance}:{RESULT_PREFIX}:{kind}:{key}"

    # -- 读写 --------------------------------------------------------------
    def get(self, *, kind: str, key: str, now: float | None = None) -> ResultHit | None:
        raw = self.kv.get(self.key_for(kind, key))
        if raw is None:
            self.misses += 1
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.warning("结果缓存反序列化失败，已删除该 key：%s", self.key_for(kind, key))
            self.kv.delete(self.key_for(kind, key))
            self.misses += 1
            return None
        moment = float(now) if now is not None else time.time()
        self.hits += 1
        return ResultHit(
            value=payload.get("value"),
            age_seconds=max(0.0, moment - float(payload.get("created_at") or moment)),
            kind=str(kind),
            key=str(key),
        )

    def set(
        self,
        *,
        kind: str,
        key: str,
        value: Any,
        ttl_seconds: int,
        now: float | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> int:
        """写一条（返回写入的字节数，便于演示脚本给出「省了多少」的直观数字）。"""
        if int(ttl_seconds) <= 0:
            raise ResultCacheError(
                f"结果缓存的 ttl_seconds 必须为正数，收到 {ttl_seconds}。"
                "想长久缓存请显式给一个大值（例如 30*86400）——"
                "让「没写 TTL」不可能静默变成「永久缓存」。"
            )
        payload = {
            "value": value,
            "created_at": float(now) if now is not None else time.time(),
            "kind": str(kind),
            "meta": dict(meta or {}),
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=_json_default)
        except TypeError as exc:
            raise ResultCacheError(
                f"结果缓存的值无法 JSON 序列化：{exc}。"
                "缓存层不做「悄悄转成字符串」这类兜底 —— 那会让读回来的值与写进去的不是同一个东西。"
            ) from exc
        self.kv.set(self.key_for(kind, key), encoded, ttl_seconds=int(ttl_seconds))
        self.writes += 1
        return len(encoded.encode("utf-8"))

    def delete(self, *, kind: str, key: str) -> bool:
        return self.kv.delete(self.key_for(kind, key)) > 0

    def purge(self, *, kind: str | None = None) -> int:
        """按 `kind` 清理（`kind=None` = 清掉本实例的全部结果缓存）。"""
        scan = getattr(self.kv, "scan_keys", None)
        if not callable(scan):
            return 0
        pattern = f"{self.namespace}:{self.instance}:{RESULT_PREFIX}:" + (f"{kind}:*" if kind else "*")
        keys = scan(pattern)
        return self.kv.delete(*keys) if keys else 0

    def state(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "namespace": f"{self.namespace}:{self.instance}:{RESULT_PREFIX}:",
        }


def _json_default(value: Any) -> Any:
    """把「几乎肯定该报错」的类型挑出来给一条能照着改的消息。

    `datetime` 是这里唯一**宽容**处理的类型（转成 ISO 字符串）：报表结果里出现
    日期是最常见的情况，而它转成字符串之后的信息量完全不丢。
    其余类型（ORM 实例、`set`、`bytes`）一律抛错 —— 它们要么有更该用的表示
    （先 `model_dump()`），要么转成字符串之后读回来没法用。
    """
    from datetime import date, datetime

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(
        f"类型 {type(value).__name__} 不可 JSON 序列化。"
        "请先转成基础类型（`model_dump()` / `asdict()` / `int()`），缓存层不做隐式转换。"
    )


__all__ = [
    "RESULT_PREFIX",
    "ResultCacheError",
    "ResultHit",
    "ResultStore",
]
