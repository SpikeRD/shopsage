"""缓存层的配置面：**每一项都有代价**，所以每一项都说清楚「配错了会怎样」。

与 `app/rag/settings.py` 同一口径：传一个 `CacheSettings` 比 monkeypatch 环境变量干净
（也不受 `get_settings()` 的 lru_cache 影响），所以本模块的每个数据类都能独立构造，
`from_settings()` 只是「读一遍 `.env`」的那条路径。

## 三个阈值之间的关系（最容易配错的地方）

```
   0 ────────────── tau_no_answer ─────────── tau_hit ───────────── 1
        一定不是同一问           |             命中区
                          「可能是，但不敢答」
```

* `semantic_threshold`（`CACHE_SIM_THRESHOLD`）—— **命中线**：相似度 ≥ 它才敢把缓存答案给用户。
  它太高 → 缓存形同虚设（命中率低、成本降不下来，E1 失效）；
  它太低 → 命中的其实是**另一个问题**（用户看到一句正确但不对题的回答，比没有缓存糟得多）。
* `semantic_floor`（`CACHE_SIM_FLOOR`）—— **兜底线**：相似度连它都不到就早退，
  连「候选最近邻」都不返回。它把「明显不是同一问」挡在更贵的路径之外。
* 两者之间是**灰区**：检索会返回候选，但 `semantic_cache_get` 判为未命中。
  灰区的存在是刻意的：**它让「阈值调太松」有一个可观测的中间地带**
  （见 `metrics.CACHE_NEAR_MISS`），而不是「命中/不命中」之间一道看不见的墙。

⚠️ `semantic_threshold` 必须是**保守**的（13.cache.md §4 第 3 条）：
它能被调对，靠的是第 14 步的 golden set，不是靠在这里拍一个数字。
所以本模块把它做成配置项并把默认值定得偏高（0.93），让「命中率低」先表现为一件事，
而不是让「答错问题」悄悄发生。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Redis key 的命名空间。队列用的是 `arq:`（arq 自己的约定），缓存用 `cache:`。
#: 多环境共用同一个 Redis 实例时只能靠它区分 —— 所以它必须出现在**每一个** key 上
#: （`backend.CacheKeySpace` 是唯一的构造处）。
KEY_NAMESPACE: Final[str] = "cache"

#: 相似度命中的默认阈值。**这个数不是猜的**：`scripts/cache_demo.py --scenario threshold`
#: 会把真实分布打出来，本机实测（`BAAI/bge-m3`，2026-10-13）：
#:
#: ```
#: 0.9865  怎么样才能退货              ← 同义改写
#: 0.9681  退货怎么操作                ← 同义改写
#: 0.9395  我想退货，流程是怎么样的      ← 同义改写（勉强过线）
#: 0.8835  退货流程是什么              ← 同一个意思，但**没过线**（见下面的警告）
#: 0.8737  退货需要什么条件
#: 0.5835  怎么改地址                  ← 不相干
#: 0.4903  你们几点上班                ← 不相干
#: ```
#:
#: 读法一：**不相干的问法落在 0.5~0.6**，而同义改写落在 0.87~0.99 —— 中间是空的，
#: 所以 0.93 这条线在实测分布上没有「误伤」（不相干的离它很远）。
#:
#: 读法二（更重要）：**「同义」与「没过线」之间没有天然的大缝** ——
#: 「退货流程是什么」与「怎么退货」语义上是一回事，实测只有 0.88。
#: 这说明命中率对措辞**很敏感**，也就是说**这条线只能靠 golden set 调**
#: （第 14 步），调它不是「拧一个旋钮」，而是「选择漏报与误报的取舍」。
#:
#: ⚠️ 换 embedding 模型（或换后端）之后这个分布会整体漂移，必须重跑那条命令重新定。
DEFAULT_SIM_THRESHOLD: Final[float] = 0.93

#: 早退兜底线：低于它就当作「完全不相干」，连候选都不返回。
#: 定得比命中线低一档（0.80）：**实测的依据**是「不相干的问法集中在 0.49~0.58」，
#: 而「相关但答案不同」的问法（例如「退货需要什么条件」，0.87）落在它上面 ——
#: 于是灰区（0.80~0.93）**正好覆盖了「像，但不敢答」的那一档**，
#: 而不是把「完全不相干」也算进来（那会让灰区指标失去意义）。
DEFAULT_SIM_FLOOR: Final[float] = 0.80

#: 三条 TTL（秒）。分档而不是一个数，因为「政策多久变一次」与「订单多久变一次」差三个数量级。
DEFAULT_TTL_SECONDS: Final[int] = 24 * 3600
#: 政策/FAQ/条款类：一周。这类文档改动是**有人编辑**触发的，不是数据流。
DEFAULT_POLICY_TTL_SECONDS: Final[int] = 7 * 24 * 3600
#: 强时效类（订单/库存/物流）的 TTL —— 只在 `no_cache` 策略选了 `short` 时才用得上。
#: 60 秒：比「用户刷新一次页面」长，比「状态真的变了」短。
DEFAULT_TIME_SENSITIVE_TTL_SECONDS: Final[int] = 60

#: 单条缓存条目里最多保留几条出处（溯源用）。
#: 为什么要有上限：一条 RAG 答案的出处引用可能很多，而它们都会被写进缓存值；
#: 无上限等于让 key 的大小由语料规模决定（一个用户可控的放大系数）。
DEFAULT_MAX_CITATIONS: Final[int] = 8

#: 常驻缓存条目的上限（内存后端用它做 LRU 淘汰；Postgres 后端用它做 `purge` 的默认上限）。
#: 2000 条 × 每条约 2KB（答案 + 向量）≈ 4MB —— 内存后端可以整份放进进程里。
DEFAULT_MAX_ENTRIES: Final[int] = 2000

#: embedding 缓存的 TTL（Redis）。它只回答「同一句话的向量是多少」，
#: 与知识库内容无关，所以可以比答案缓存活得久 —— 但**不能永不过期**：
#: 换模型会让旧向量变成噪音，而 key 里已经带了模型名（见 `backend.CacheKeySpace`）。
DEFAULT_EMBEDDING_TTL_SECONDS: Final[int] = 7 * 24 * 3600


@dataclass(frozen=True)
class CacheSettings:
    """缓存的行为参数（**都是语义选择，不是环境差异**）。

    为什么 timeout 在这里而不是在 Redis 客户端上：缓存的可用性目标是「**永不拖慢主路径**」。
    一次缓存查询必须在一个很短的上限内失败并回落（`FailOpen`）——
    把超时交给客户端的默认值（可能几十秒）会让「Redis 卡住」变成「接口全挂」，
    而缓存的全部意义是让接口更快。
    """

    enabled: bool = True
    #: 相似度命中线 / 早退兜底线（见模块 docstring 的图）
    semantic_threshold: float = DEFAULT_SIM_THRESHOLD
    semantic_floor: float = DEFAULT_SIM_FLOOR
    #: 三条 TTL
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    policy_ttl_seconds: int = DEFAULT_POLICY_TTL_SECONDS
    time_sensitive_ttl_seconds: int = DEFAULT_TIME_SENSITIVE_TTL_SECONDS
    #: 强时效问题（订单/库存/物流）的处置：`bypass` = 完全不缓存，`short` = 只缓存极短时间。
    #: 13.cache.md §4 第 2 条两者都允许；默认选 `bypass`（更保守，且「没有缓存」这件事
    #: 不可能被误读成「缓存里是旧值」）。
    time_sensitive_policy: str = "bypass"
    #: 单条条目的出处条数上限
    max_citations: int = DEFAULT_MAX_CITATIONS
    #: 常驻条目上限（内存后端的 LRU 容量）
    max_entries: int = DEFAULT_MAX_ENTRIES
    #: 超时（秒）：查询与写入各自的上限
    lookup_timeout_seconds: float = 0.5
    write_timeout_seconds: float = 1.0
    #: Redis 不可用时**是否回落到进程内存**。
    #: 默认 True（开发机上没有 Redis 也能把链路跑通），但它是一条**日志里必须看得见**的降级：
    #: `backend.MemoryKeyValue` 会在首次回落时打 WARNING，`/health` 的组件状态也会显示
    #: `degraded`。生产想强制「没有 Redis 就不缓存」就把它设成 False。
    fallback_to_memory: bool = True
    #: 语义匹配的最大候选数（Postgres 侧 `order by embedding <=> ... limit n`）。
    #: 只要 5 足够（第 1 名用来判命中，其余用于排障）。
    semantic_candidates: int = 5
    #: 是否把命中/未命中写进指标（压测对照时可以关）
    metrics_enabled: bool = True

    def ttl_for(self, tier: str) -> int:
        """按分档取 TTL（未知分档回落到 `ttl_seconds`，**不抛**：分档是提示不是契约）。"""
        return {
            "policy": int(self.policy_ttl_seconds),
            "time_sensitive": int(self.time_sensitive_ttl_seconds),
        }.get(str(tier), int(self.ttl_seconds))

    def validate(self) -> list[str]:
        """自检：返回问题清单（空 = 一切就绪）。

        为什么返回清单而不是抛异常：它同时被 `/health`、演示脚本与测试用，
        而「配置不合理」在开发机上应该是**一句能看见的告警**，不是启动失败
        （与 `queue.worker.selfcheck` 同一口径）。
        """
        problems: list[str] = []
        if not (0.0 < self.semantic_floor <= self.semantic_threshold < 1.0):
            problems.append(
                f"相似度阈值不合理：floor={self.semantic_floor} threshold={self.semantic_threshold}"
                "（必须满足 0 < floor <= threshold < 1）。"
                "floor > threshold 会让灰区变成负数（永远早退），threshold >= 1 则永远不命中。"
            )
        if self.time_sensitive_policy not in ("bypass", "short"):
            problems.append(
                f"强时效策略只能是 bypass / short，收到 {self.time_sensitive_policy!r}"
                "（写别的值等于默认走 bypass，但那是**猜**出来的语义，不如报出来）"
            )
        for name, value in (
            ("ttl_seconds", self.ttl_seconds),
            ("policy_ttl_seconds", self.policy_ttl_seconds),
            ("time_sensitive_ttl_seconds", self.time_sensitive_ttl_seconds),
        ):
            if int(value) <= 0:
                problems.append(f"{name} 必须为正数，收到 {value}（TTL<=0 等于永不写入）")
        if self.time_sensitive_policy == "short" and self.time_sensitive_ttl_seconds >= self.ttl_seconds:
            problems.append(
                "强时效 TTL 不小于普通 TTL：这等于仍然按普通时长缓存强时效数据（F1 失效）。"
                f"time_sensitive={self.time_sensitive_ttl_seconds} ttl={self.ttl_seconds}"
            )
        return problems


@dataclass(frozen=True)
class CacheBackendSettings:
    """传输层参数（Redis 地址、key 前缀、超时）。"""

    redis_url: str = ""
    #: key 前缀：`cache:sem:v1:...`（多环境隔离的落点，见 `KEY_NAMESPACE`）
    namespace: str = KEY_NAMESPACE
    #: 逻辑库/实例的标识：进 key，让「同一个 Redis 上的两个部署」不互相命中
    instance: str = "default"
    embedding_ttl_seconds: int = DEFAULT_EMBEDDING_TTL_SECONDS

    @classmethod
    def from_settings(cls, settings: Any = None) -> CacheBackendSettings:
        """从全局配置读（`redis_url` 是唯一真正来自 `.env` 的项）。"""
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        env = str(getattr(settings, "env", "dev") or "dev")
        return cls(
            redis_url=str(getattr(settings, "redis_url", "") or ""),
            instance=env,
        )


@dataclass(frozen=True)
class CacheConfig:
    """本模块**唯一**的「配置整体」出口（缓存 + 传输层）。

    为什么把两者包成一个对象：`semantic_cache_set` 这类门面函数要同时用它们，
    而「读两次全局配置」在热路径上是两次 `lru_cache` 查表 + 两处可能漂移的默认值。
    一个对象让「这一次调用用的是哪份配置」变成**一个**变量。
    """

    cache: CacheSettings
    backend: CacheBackendSettings

    @classmethod
    def from_settings(
        cls,
        settings: Any = None,
        *,
        cache: CacheSettings | None = None,
        backend: CacheBackendSettings | None = None,
    ) -> CacheConfig:
        return cls(
            cache=cache or CacheSettings(),
            backend=backend or CacheBackendSettings.from_settings(settings),
        )

    def problems(self) -> list[str]:
        return self.cache.validate()


#: 进程默认配置（懒建：`from_settings` 要读 `.env`，不该在 import 期发生）。
_DEFAULT: CacheConfig | None = None


def default_config() -> CacheConfig:
    """取进程默认配置（`/health`、门面函数的默认值都走它）。

    ⚠️ 每次调用都重新读 `.env` 太贵（热路径上每个请求都会来一次），
    但**缓存住**又会让测试里改了配置不生效。折中：这里每次都构造 ——
    `get_settings()` 自己有 lru_cache，所以真实开销只是几次属性读取。
    """
    return CacheConfig.from_settings()


__all__ = [
    "DEFAULT_EMBEDDING_TTL_SECONDS",
    "DEFAULT_MAX_CITATIONS",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_POLICY_TTL_SECONDS",
    "DEFAULT_SIM_FLOOR",
    "DEFAULT_SIM_THRESHOLD",
    "DEFAULT_TIME_SENSITIVE_TTL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "KEY_NAMESPACE",
    "CacheBackendSettings",
    "CacheConfig",
    "CacheSettings",
    "default_config",
]
