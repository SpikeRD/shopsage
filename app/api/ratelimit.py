"""限流：**按「调用者 + 接口」计数的滑动窗口**（`9.api.md` §4 第 3 条、D1 纵深）。

为什么限流是 API 层的事、而不是模型层的事（`9.api.md` §5 第三条）：

| 层 | 拦的是什么 | 超限之后 |
|---|---|---|
| 本模块（API 层） | **用户请求**（一个人刷接口） | 429，请求根本不会进图 —— 零模型成本、零数据库代价 |
| `app/llm/`（第 5 步） | **模型调用**（一次请求里的某次调用 429/超时） | 指数退避重试，用户无感 |

两处都叫「限流」，但一个是「别让你进来」，一个是「进来了我重试」。
混在一起的后果是：把用户刷接口当成上游抖动去退避重试 —— 于是越刷越慢、还越刷越花钱。

## 为什么用**滑动窗口**而不是令牌桶

本项目的诉求是「防滥用与刷接口」（D1 纵深），不是「平滑突发流量整形」：

* 滑动窗口的语义**一眼可解释**：「最近 60 秒内你只能发 30 次」—— 用户、客服、评审都懂；
* 令牌桶多了「桶容量 / 补充速率」两个参数，多出来的那点突发容忍度在本场景没有用；
* 实现只要一个「带时间戳的队列」，Redis 侧是 ZSET，**没有需要调的参数**。

## 为什么必须同时提供 Redis 与内存两个实现

* `redis`：生产口径。API 是**无状态多副本**（架构第 8 节），计数器必须在进程外，
  否则 N 个副本 = 每个副本各放行 N 倍流量（限流形同虚设）；
* `memory`：Redis 没起来时的**降级**。理由不是「方便测试」——
  而是「限流后端挂了」有两种处置：① 一律 503（安全但整站不可用）、② 放行但告警。
  对客问答这种**只读**接口选 ②，代价是「限流暂时失效」，收益是「服务还在」。
  ⚠️ 但**降级是有声音的**：`RateLimiter.backend_name` 会进日志、进 `/health`、进响应头，
  不允许静默降级。

## 为什么桶键里带角色

`u:2:customer:1:chat` 这个键的形状让 Redis 里一眼能看出「谁在刷、用什么身份」。
`role` 来自**已验签的令牌**（`Principal.role`），不是请求体 —— 否则谁都能把自己标成 `admin`
去蹭另一个桶的配额。匿名请求（无令牌）按来源 IP 分桶，顺带把「未鉴权刷接口」也压住。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Final, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: 桶键里代表「没带令牌」的那一段（按 IP 分桶）。
ANONYMOUS: Final[str] = "anon"

#: 桶键前缀（Redis 里一眼能认出是我们的键，便于 `SCAN` 排查）
KEY_PREFIX: Final[str] = "shopsage:rl"

#: Redis 键的存活时间倍数：窗口 × 这个系数。留冗余是为了「窗口内的时间戳还在、计数却不丢」，
#: 又不能让键永远留着（那会攒出一堆僵尸键）。
KEY_TTL_FACTOR: Final[int] = 2


@dataclass(frozen=True)
class RateLimitRule:
    """一条限流规则：**窗口内最多几次**。

    三个字段都刻意用「人能读的整数」表达，不做「每秒补充 X 个令牌」这种需要换算的写法 ——
    排障时不该需要计算器。
    """

    limit: int
    window_seconds: float

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError(f"limit 必须为正整数（收到 {self.limit}）：0 或负数会让接口永远 429")
        if self.window_seconds <= 0:
            raise ValueError(f"window_seconds 必须为正数（收到 {self.window_seconds}）")

    @property
    def ttl_seconds(self) -> int:
        """Redis 键的过期秒数（见 `KEY_TTL_FACTOR`）。"""
        return max(1, int(self.window_seconds * KEY_TTL_FACTOR))


#: 未被任何规则覆盖的接口用这条（宽松但**有**，好过没有 —— 见 `RateLimiter.rule_for`）。
DEFAULT_RULE: Final[RateLimitRule] = RateLimitRule(limit=60, window_seconds=60.0)

#: 接口 → 规则。**默认拒绝遗漏**：没登记的接口退回 `DEFAULT_RULE`（而不是不限流）。
DEFAULT_RULES: Final[dict[str, RateLimitRule]] = {
    # 对客问答是**最贵**的接口（一次请求 = 若干次模型调用 + 一次 SQL），
    # 30 次/分钟对正常用户足够（人打不了这么快），对脚本刷接口足够紧。
    "chat": RateLimitRule(limit=30, window_seconds=60.0),
    # 流式：比非流式更宽松一点（一个页面可能开多条连接），但仍要封顶 ——
    # 一条 WS 连接占一个长连接名额，不限流就是最简单的资源耗尽攻击面。
    "chat.stream": RateLimitRule(limit=60, window_seconds=60.0),
    # 批准通道：写操作的手工确认，频次天然很低；给个明确的小额度而不是不限。
    # ⚠️ 变更 003 起 `chat.approve` 这条额度**已随接口一起删除**（对客闸门整体移除）。
    # 保留一个没有任何路径指向的额度常量，只会让 `SCOPE_BY_ROUTE/.env.example/
    # DEFAULT_RULES` 三处的「哪些接口受限流」出现分歧 —— 那正是本模块 docstring
    # 第一段要防的事。
    # 长任务提交（第 12 步）：**比 chat 更严**。一次提交换来一个后台长任务
    # （几分钟、若干次模型调用），而被滥用的代价落在**队列**上 ——
    # 积压会让所有人的任务一起变慢，不像 chat 那样只影响调用者自己。
    "tasks": RateLimitRule(limit=10, window_seconds=60.0),
    # 健康检查：探针本身就可能每几秒来一次，给宽松额度（这条限流拦的是「有人拿 /health 当压力入口」）。
    "health": RateLimitRule(limit=120, window_seconds=60.0),
    # 指标：给 Prometheus 抓取留足（scrape_interval 15s → 4 次/分钟就够）。
    "metrics": RateLimitRule(limit=120, window_seconds=60.0),
    # ── 变更 001：账号接口 ────────────────────────────────────────────────────
    # 登录：**全项目最严的一条**。它是唯一能用「猜」换到令牌的入口，
    # 而不限流就等于给暴力破解无限次尝试。10 次/5 分钟对人足够
    # （正常人不会 5 分钟内输错 10 次），对脚本则把爆破速率压到没有意义。
    # ⚠️ 未登录时桶键只能退回**来源 IP**（没有令牌可解析），所以同一出口 NAT 后面的
    # 多个用户会共用这一额度 —— 这是本决策的已知代价，比「不限流」小得多。
    "auth.login": RateLimitRule(limit=10, window_seconds=300.0),
    # 刷新：比登录松（它是正常用户的常规动作，14 天有效期内每天可能刷几次），
    # 但仍要封顶 —— 否则它就是一个「可无限尝试的凭证校验口」。
    "auth.refresh": RateLimitRule(limit=60, window_seconds=300.0),
    # ── 变更 003：退款申请与审批 ──────────────────────────────────────────────
    # 申请：偏紧。一次调用会在库里多一行、并**占用该订单的可退额度**（有限资源）。
    # 20 次/分钟对人绰绰有余（正常客户一次退款只点一次），对脚本则压得住
    # 「把某客户所有订单在几秒内全申请一遍」这种灌库行为。
    "refunds.request": RateLimitRule(limit=20, window_seconds=60.0),
    # 审批：比申请更松但仍有上限（一线客服批量处理待办是正常动作，
    # 一个人一分钟点十几个「通过」完全可能；而它封的是脚本化的批量改状态）。
    "refunds.decide": RateLimitRule(limit=60, window_seconds=60.0),
}


@dataclass(frozen=True)
class RateLimitDecision:
    """一次判定的结果。`allowed=False` 时 `retry_after` 一定 ≥ 1（秒，可直接进响应头）。"""

    allowed: bool
    limit: int
    window_seconds: float
    remaining: int
    retry_after: int = 0

    def headers(self) -> dict[str, str]:
        """给 HTTP 响应/WS 帧用的头字段（**统一形状**，前端只需按名字读）。

        `X-RateLimit-*` 是业界事实标准（GitHub / Stripe 等都在用），
        比自定义名字好：前端现有的限流展示组件能直接吃。
        """
        out = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Window": str(int(self.window_seconds)),
        }
        if not self.allowed:
            out["Retry-After"] = str(self.retry_after)
        return out


class RateLimitStore(Protocol):
    """计数后端（Redis / 内存两种实现，接口一致）。"""

    name: str

    def hit(self, key: str, rule: RateLimitRule) -> RateLimitDecision:
        """记一次访问并判定是否放行。"""
        ...

    def reset(self, key: str | None = None) -> None:
        """清空计数（`key=None` 清全部）。测试与排障用。"""
        ...


class RateLimitRuntime(Protocol):
    """运行时可替换的限流入口（`app.state.rate_limiter` 的类型）。"""

    @property
    def store(self) -> RateLimitStore:
        ...

    def rule_for(self, scope: str) -> RateLimitRule:
        ...

    def check(self, caller: str, scope: str) -> RateLimitDecision:
        ...

    def backend_name(self) -> str:
        ...


def caller_bucket(*, user_id: int | None, role: str = "", client_ip: str = "") -> str:
    """造桶键的「谁」那一段。

    顺序：有身份 → `u:<id>:<role>`；无身份 → `ip:<来源 IP>`；都没有 → `anon`。
    ⚠️ 无身份时**不能**退回「共用一个 anon 桶」：那会让一个匿名脚本把所有匿名用户一起限死
    （一个攻击者能影响一片人），所以退回来源 IP。
    """
    if user_id is not None:
        return f"u:{int(user_id)}:{role or 'unknown'}"
    if client_ip:
        return f"ip:{client_ip}"
    return ANONYMOUS


class MemoryRateLimitStore:
    """进程内滑动窗口：`key -> 窗口内的时间戳队列`。

    为什么用 `deque` 而不是「计数器 + 定时重置」：固定窗口在窗口边界会放行 **2 倍**流量
    （59.9s 打满、60.1s 再打满），而滑动窗口不会 —— 多存几个时间戳就能消掉这个缺口。

    ⚠️ 锁是必须的：FastAPI 的同步依赖跑在线程池里，多个请求可能真的并发进这里。
    代价是每进程一把锁（没有共享内存）—— 所以它**只是降级实现**，生产口径是 Redis。
    """

    name: Final[str] = "memory"

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def hit(self, key: str, rule: RateLimitRule) -> RateLimitDecision:
        now = self._clock()
        cutoff = now - rule.window_seconds
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= rule.limit:
                # 最早那次离开窗口的时刻 = 可以再发的时刻
                retry_after = max(1, int(round(hits[0] + rule.window_seconds - now)) + 1)
                return RateLimitDecision(
                    allowed=False,
                    limit=rule.limit,
                    window_seconds=rule.window_seconds,
                    remaining=0,
                    retry_after=retry_after,
                )
            hits.append(now)
            return RateLimitDecision(
                allowed=True,
                limit=rule.limit,
                window_seconds=rule.window_seconds,
                remaining=max(0, rule.limit - len(hits)),
            )

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class RedisRateLimitStore:
    """Redis 滑动窗口：`ZSET` 存时间戳，`ZREMRANGEBYSCORE` 清窗口外，`ZCARD` 计数。

    为什么是 ZSET 而不是 `INCR` + `EXPIRE`（固定窗口）：见 `MemoryRateLimitStore` 的注释 ——
    固定窗口在边界上放行双倍流量。

    ⚠️ **每次判定要 3 个往返**（清、数、加）。这里用 pipeline 压成 1 个往返，
    并且刻意**不用 Lua**：Lua 更快，但它把「限流逻辑」藏进了一段字符串，
    而这段逻辑正是最需要被读的部分（评审时没人会去读 `EVAL` 里的脚本）。
    3 条命令的原子性缺口（并发下可能多放行一两个请求）在本场景可以接受 ——
    目标是「挡住刷接口」，不是「精确到个位的配额结算」。
    """

    name: Final[str] = "redis"

    def __init__(self, client: object) -> None:
        self._client = client

    def hit(self, key: str, rule: RateLimitRule) -> RateLimitDecision:
        import time as _time

        now = _time.time()
        cutoff = now - rule.window_seconds
        try:
            pipe = self._client.pipeline()  # type: ignore[attr-defined]
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zadd(key, {f"{now:.6f}": now})
            pipe.zcard(key)
            pipe.zrange(key, 0, 0, withscores=True)  # 最早那次（算 Retry-After 用）
            pipe.expire(key, rule.ttl_seconds)
            results = pipe.execute()
            used = int(results[2])
            oldest = results[3]
        except Exception as exc:  # noqa: BLE001 - 后端故障不能把请求打成 500
            logger.warning("Redis 限流判定失败（按放行处理，见本模块 docstring）：%s", exc)
            return RateLimitDecision(
                allowed=True,
                limit=rule.limit,
                window_seconds=rule.window_seconds,
                remaining=rule.limit,
            )
        if used > rule.limit:
            # Retry-After 取自**窗口内最早那一次**离开窗口的时刻（而不是笼统的 window 长度）：
            # 前者是「你最早什么时候能再发」，后者会把等待时间夸大好几倍。
            earliest = float(oldest[0][1]) if oldest else now
            return RateLimitDecision(
                allowed=False,
                limit=rule.limit,
                window_seconds=rule.window_seconds,
                remaining=0,
                retry_after=max(1, int(earliest + rule.window_seconds - now) + 1),
            )
        return RateLimitDecision(
            allowed=True,
            limit=rule.limit,
            window_seconds=rule.window_seconds,
            remaining=max(0, rule.limit - used),
        )

    def reset(self, key: str | None = None) -> None:
        try:
            if key is None:
                for found in self._client.scan_iter(f"{KEY_PREFIX}:*"):  # type: ignore[attr-defined]
                    self._client.delete(found)  # type: ignore[attr-defined]
            else:
                self._client.delete(key)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 限流计数清空失败：%s", exc)


class RateLimiter:
    """把「规则表 + 计数后端」装配成 `check(caller, scope)` 一处入口。"""

    def __init__(
        self,
        *,
        store: RateLimitStore,
        rules: dict[str, RateLimitRule] | None = None,
        default_rule: RateLimitRule | None = None,
        key_prefix: str = KEY_PREFIX,
    ) -> None:
        self.store = store
        self.rules = dict(rules or DEFAULT_RULES)
        self.default_rule = default_rule or DEFAULT_RULE
        self.key_prefix = key_prefix

    def rule_for(self, scope: str) -> RateLimitRule:
        """取某个接口的规则（未登记的接口退回默认值，**不是不限流**）。"""
        rule = self.rules.get(scope)
        if rule is None:
            logger.debug("接口 %r 未登记限流规则，退回默认 %s", scope, self.default_rule)
            return self.default_rule
        return rule

    def check(self, caller: str, scope: str) -> RateLimitDecision:
        rule = self.rule_for(scope)
        return self.store.hit(f"{self.key_prefix}:{scope}:{caller}", rule)

    def backend_name(self) -> str:
        return self.store.name


def open_rate_limiter(
    *,
    redis_url: str = "",
    rules: dict[str, RateLimitRule] | None = None,
    connect_timeout: float = 0.5,
) -> RateLimiter:
    """造一个限流器：能连上 Redis 就用 Redis，连不上**降级到内存并告警**。

    ⚠️ 探测必须带**短超时**：`redis.from_url` 本身不连，第一次命令才连 ——
    不加超时的话，Redis 主机不可达时首个请求会卡在 TCP 超时上（本机实测数秒），
    而这段时间整站都在等这一条限流判定。0.5 秒足够本机/同网段，也不至于让首请求明显变慢。

    ⚠️ 连不上**不抛异常**：见模块 docstring 的取舍 —— 对客只读接口选「降级放行 + 大声告警」。
    想要「Redis 挂了就整站不可用」的话，那是可用性策略选择，应该在编排层（K8s probe / 网关）
    表达，而不是藏在一个限流器的初始化里。
    """
    if not redis_url:
        logger.warning("未配置 REDIS_URL：限流降级到**进程内**计数（多副本下每个副本各限一份，见模块 docstring）")
        return RateLimiter(store=MemoryRateLimitStore(), rules=rules)

    try:
        import redis as redis_sync

        client = redis_sync.Redis.from_url(
            redis_url,
            socket_connect_timeout=connect_timeout,
            socket_timeout=connect_timeout,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 连不上只降级，不阻断启动
        logger.warning(
            "Redis 不可达（%s: %s）：限流降级到**进程内**计数 —— 多副本部署时每个副本各限一份，"
            "真实额度会被放大到「副本数 × limit」。请检查 REDIS_URL（见 docs/runbook/2.infra-runbook.md）",
            type(exc).__name__,
            exc,
        )
        return RateLimiter(store=MemoryRateLimitStore(), rules=rules)

    logger.info("限流后端 = redis（多副本共享同一份计数）")
    return RateLimiter(store=RedisRateLimitStore(client), rules=rules)


async def async_check(limiter: RateLimitRuntime, caller: str, scope: str) -> RateLimitDecision:
    """异步上下文里安全地做一次判定。

    为什么要有这个包装：Redis 客户端是**同步**的，直接在 `async def` 路由里调它会
    **阻塞事件循环**（`5.llm-runbook.md` §10 记过同一个坑：整站卡住，不只这一个请求）。
    所以同步判定一律丢进线程池。

    内存实现被一起丢进线程池是**有意**的：这样「限流的执行模型」只有一种，
    `memory` 与 `redis` 的行为差异不会在并发压测里才暴露出来。
    """
    return await asyncio.to_thread(limiter.check, caller, scope)


__all__ = [
    "ANONYMOUS",
    "DEFAULT_RULE",
    "DEFAULT_RULES",
    "KEY_PREFIX",
    "MemoryRateLimitStore",
    "RateLimitDecision",
    "RateLimitRule",
    "RateLimitRuntime",
    "RateLimitStore",
    "RateLimiter",
    "RedisRateLimitStore",
    "async_check",
    "caller_bucket",
    "open_rate_limiter",
]
