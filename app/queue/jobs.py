"""任务契约：**任务名、入参形态、重试策略、幂等键**（本模块不碰 Redis，也不跑模型）。

`12.queue.md` §6 第 1 步要的「`WorkerSettings`：任务注册 + 重试参数」拆成了两个文件，
因为那两件事的**真相来源不同**：

| 内容 | 放哪 | 为什么 |
|---|---|---|
| 任务名 / 入参形状 / 重试策略 / 幂等键 | **本文件** | 纯数据与纯函数：不需要 Redis、不需要模型，所以能被离线单测逐条钉住 |
| worker 进程怎么起、注册哪些函数 | `worker.py` | 只有那里知道 arq 的存在 |

这样拆的直接收益：`app/queue/core.py`（重试与死信的判定）可以**完全不 import arq**，
于是「连续失败 N 次 → 进死信」「死信可重放」这两条队列语义能在内存里验完，
不必等 Redis 起来（与第 7、8 步「离线可测」是同一条纪律）。

## 幂等键（A1/A2 的落点）

`JobSpec.idempotency_key()` 只做一件事：**把「这次业务请求」映射成一个稳定字符串**。
它必须是**纯函数** —— 同一个业务请求重投两次，得到同一个 key，于是 `job_id` 相同，
arq 的 `enqueue_job(_job_id=...)` 第二次会返回 `None`（任务已存在，不重复入队）。

三类 key 的取舍：

* **显式 key**（调用方给了）：原样用。用于「我知道这次请求的业务幂等键」的场景
  （例如图侧那一轮问答的 `chat:<thread_id>:<轮次>`）；
* **业务字段派生**（`user_id` + `params` 的规范 JSON + 任务名）：调用方没给 key 时兜底。
  规范化（`sort_keys` + 紧凑分隔符）是必须的 —— 否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}`
  会派生出两个不同的 key，同一个请求投两次就真的跑两次；
* **永不用随机数/时间戳**：那等于「每次都算新请求」。这条在 `8.graph-runbook.md` §5.3
  已经为写路径定过口径，队列侧是同一个坑的另一面。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, Literal

#: 任务种类（写进死信记录、指标标签、API 的入参校验）。
#:
#: ⚠️ 它是一个**封闭取值域**：`enqueue` 的 API 只接受这三个值，
#: 这样「队列里都有哪几类活」是一个可枚举的事实，而不是散在 payload 里的自由文本。
JobKind = Literal["report", "agent", "kb_rebuild"]

JOB_KINDS: Final[tuple[str, ...]] = ("report", "agent", "kb_rebuild")

#: 任务名 → arq 注册名。**名字是持久化契约**：它被写进 Redis 的 job 数据，
#: 改名会让「重启前入队、重启后执行」的任务找不到函数（症状是 arq 日志里的
#: `function 'x' not found`，任务直接进 failed）。
FUNC_REPORT: Final[str] = "shopsage.run_report"
FUNC_AGENT: Final[str] = "shopsage.run_agent_turn"
FUNC_KB_REBUILD: Final[str] = "shopsage.rebuild_knowledge_base"

FUNCTIONS: Final[tuple[str, ...]] = (FUNC_REPORT, FUNC_AGENT, FUNC_KB_REBUILD)

KIND_TO_FUNCTION: Final[dict[str, str]] = {
    "report": FUNC_REPORT,
    "agent": FUNC_AGENT,
    "kb_rebuild": FUNC_KB_REBUILD,
}

#: 幂等键允许的字符集长度（进 Redis 的 key 与 DB 的列，太长的 key 既是噪音也是风险）
MAX_KEY_CHARS: Final[int] = 64

#: `job_id` 的前缀：**它是 Redis 里可辨认的名字空间**，也是「这个 id 是我们发的」的凭据。
#:
#: ⚠️ 为什么不直接用调用方给的 key 当 job_id：arq 的 job_id 会进 redis key
#: （`arq:job:<id>`、`arq:result:<id>`），而调用方的 key 可能是任意字符串（含空格、冒号、
#: 甚至非 ASCII 的会话 id）。加一层前缀 + 只允许安全字符，让「key 长什么样」不成为
#: Redis 的行为的一部分。
JOB_ID_PREFIX: Final[str] = "q_"


@dataclass(frozen=True)
class RetryPolicy:
    """一个任务的**重试分级**（`12.queue.md` §4 第 3 条）。

    三个字段各有各的用途，且都能被单独说清：

    | 字段 | 作用 | 不设的后果 |
    |---|---|---|
    | `attempts` | 总共允许执行几次（含首次） | 无限重试 |
    | `base_delay` | 第一次退避的秒数 | 立刻重试 = 打爆下游 |
    | `max_delay` | 退避的上限 | 长指数退避退到「下个世纪才重试」 |
    | `jitter` | 退避的抖动系数（0~1） | 一批同时失败的任务**同时**回来（惊群） |

    ## 退避公式

    `delay(n) = min(base_delay * 2^(n-1), max_delay) * (1 ± jitter)`

    * `n = 1` 是**首次失败之后**的那次延迟（第 n 次重投前等多久）；
    * 抖动只在 `jitter > 0` 时生效，且是**对称**的（0.2 → 0.8x ~ 1.2x）：
      只向下抖会让实际退避总比配置的短，是另一类「配置与行为不一致」。

    ## 为什么用乘性退避而不是固定的轮询间隔

    429/限流场景下，固定间隔重试等于「以固定速率持续撞墙」；乘性退避才有机会
    让下游缓过来。这与 `app/llm/client.py` 的重试策略同源（那里是秒级、这里是分钟级）。

    ⚠️ `jitless()` 单独一个构造方法，是因为**测试需要确定性**：断言
    「退避是 1s → 2s → 4s」时不能容忍随机抖动。生产默认带抖动（见 `DEFAULT_POLICIES`），
    测试与演示用 `jitless()` —— 这不是「测试专用后门」，而是「把随机性显式化」。
    """

    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 300.0
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"attempts 必须 >= 1（收到 {self.attempts}）：0 次执行等于任务从不运行")
        if self.base_delay < 0:
            raise ValueError(f"base_delay 不能为负（收到 {self.base_delay}）")
        if self.max_delay < self.base_delay:
            raise ValueError(
                f"max_delay({self.max_delay}) 不能小于 base_delay({self.base_delay})："
                "那会让退避实际上被钉在上限，指数退避形同虚设"
            )
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError(f"jitter 必须在 [0, 1]（收到 {self.jitter}）：1.0 = 抖动一倍")

    @classmethod
    def jitless(cls, *, attempts: int = 3, base_delay: float = 1.0, max_delay: float = 300.0) -> "RetryPolicy":
        """无抖动的同一条策略（测试 / 演示用，见 docstring 末尾的说明）。"""
        return cls(attempts=attempts, base_delay=base_delay, max_delay=max_delay, jitter=0.0)

    def delay_for(self, attempt: int) -> float:
        """第 `attempt` 次尝试**失败之后**该等多久（秒）。

        `attempt=1` = 首次失败 → 返回 `base_delay`。`attempt >= attempts` 时不该再重试，
        所以这里返回 0（调用方应当先 `should_retry`）—— 返回一个大数会让
        「判定写反」这类 bug 变成一次「等了五分钟才发现」的排障。
        """
        if not self.should_retry(attempt):
            return 0.0
        raw = self.base_delay * (2 ** (attempt - 1))
        capped = min(raw, self.max_delay)
        if self.jitter <= 0.0:
            return capped
        return _jittered(capped, self.jitter)

    def should_retry(self, attempt: int) -> bool:
        """这次失败之后还该不该重试（`attempt` = 刚刚失败的这一次的序号，从 1 起）。"""
        return 1 <= attempt < self.attempts


def _jittered(seconds: float, jitter: float) -> float:
    """对称抖动：`seconds * U(1-jitter, 1+jitter)`，且**永不为负**。

    ⚠️ `jitter=1.0` 时下界是 0 —— 0 秒延迟是合法的（立刻重投），
    但负数不是（`_defer_by` 为负会让 arq 把任务当成「早就该跑」的，也就是立刻执行，
    看起来没事，实际是抖动配置写错时的静默行为漂移）。所以夹到 0。
    """
    import random

    low = max(0.0, 1.0 - jitter)
    high = 1.0 + jitter
    return max(0.0, seconds * random.uniform(low, high))


#: 三类任务的重试策略（**按代价与可恢复性分开**，不是一套参数套所有任务）。
#:
#: | 任务 | attempts | base_delay | 为什么 |
#: |---|---|---|---|
#: | `report` | 3 | 2s | 报表是「读多写少」的批处理：重试便宜、失败常见于限流/瞬时不可用 |
#: | `agent` | 2 | 5s | 一轮 Agent 会花多次模型调用（C1 成本）：第二次就改不好的，第三次也一样 |
#: | `kb_rebuild` | 5 | 30s | 重建知识库是长任务且**幂等**（整库重算）：值得多等几轮，别让它进人工队列 |
DEFAULT_POLICIES: Final[dict[str, RetryPolicy]] = {
    FUNC_REPORT: RetryPolicy(attempts=3, base_delay=2.0, max_delay=120.0, jitter=0.2),
    FUNC_AGENT: RetryPolicy(attempts=2, base_delay=5.0, max_delay=60.0, jitter=0.2),
    FUNC_KB_REBUILD: RetryPolicy(attempts=5, base_delay=30.0, max_delay=600.0, jitter=0.2),
}

#: 任务超时（秒）：超过它 arq 会取消这次执行，并按「可重试」处理。
#:
#: 为什么**必须**有上限：没有超时的长任务会把一个 worker 槽位永久占住，
#: 而队列的扩缩信号（积压）会给出「加 worker」的错误建议 —— 加了也不够，因为槽位是被卡住的。
JOB_TIMEOUT_SECONDS: Final[float] = 300.0

#: 结果在 Redis 里保留多久（秒）。24 小时足够「客户端第二天回来捞结果」，
#: 又不会让 `arq:result:*` 无限堆积（长任务的返回值可能很大）。
RESULT_TTL_SECONDS: Final[int] = 86_400

#: 延迟任务的最大提前量（秒）：`defer_by` 超过它就拒绝入队。
#:
#: 30 天是 arq 的 `_expires` 默认值（24 小时 + 延迟）附近的一个数量级 ——
#: 更远的「定时任务」应当用 cron（`worker.py` 的 `cron_jobs`），
#: 而不是一个「在 Redis 里躺一个月」的一次性任务（那类任务在重启/清库时最难发现）。
MAX_DEFER_SECONDS: Final[float] = 30 * 24 * 3600.0


@dataclass(frozen=True)
class JobSpec:
    """一次入队的全部描述（**调用方给的东西的完全展开**，之后不再做判断）。"""

    kind: JobKind
    function: str
    payload: dict[str, Any]
    idempotency_key: str
    user_id: int | None = None
    #: 延迟多久执行（秒）。`> 0` 走 arq 的 `_defer_by`（延迟队列），`0` 走立即执行队列。
    defer_by: float = 0.0
    #: 覆盖默认重试策略（`None` = 用 `policy_for()` 的默认值）
    policy: RetryPolicy | None = None

    @property
    def job_id(self) -> str:
        """入队用的 `job_id`（= 幂等键的稳定映射，见模块 docstring）。"""
        return job_id_for(self.idempotency_key)

    def resolved_policy(self) -> RetryPolicy:
        return self.policy or policy_for(self.function)


def policy_for(function: str) -> RetryPolicy:
    """取某个任务的重试策略（未登记的任务用**最保守**的一档）。

    ⚠️ 未登记时给 `attempts=1` 而不是「默认 3 次」：一个没登记策略的任务
    多半是新加的、还没想过「失败了怎么办」的 —— 对它重试等于用真实流量做实验。
    「不重试、直接进死信」会让它**立刻在死信队列里露出来**，那是更安全的默认值。
    """
    return DEFAULT_POLICIES.get(function, RetryPolicy(attempts=1, base_delay=0.0, max_delay=0.0, jitter=0.0))


def normalize_key(raw: str) -> str:
    """幂等键归一：去空白、限长、**只允许安全字符**。

    ⚠️ 非法输入**抛错**而不是「清洗成合法的」：静默清洗会让两个不同的业务请求
    撞成同一个 key（例如 `"a b"` 与 `"ab"` 都被清成 `ab`），而症状是
    「其中一个请求的结果被当成另一个的」—— 这比拒绝入队难查得多。

    ⚠️ 允许集是**显式的 ASCII 白名单**：用 `str.isalnum()` 会把中文、
    全角字符、带重音的字母都放进来（`"键".isalnum()` 是 `True`），
    而这些字符在 Redis key、日志、URL 里的表现各不相同 —— 一个「看起来能用」
    但每个环节都要转义一遍的键，不值得为它放宽规则。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("幂等键不能为空：它决定「同一个请求只执行一次」，空键会退化成随机 job_id")
    if len(text) > MAX_KEY_CHARS:
        raise ValueError(f"幂等键过长（{len(text)} > {MAX_KEY_CHARS}）：{text[:32]}…")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
    bad = sorted({ch for ch in text if ch not in allowed})
    if bad:
        raise ValueError(
            f"幂等键只允许 ASCII 的字母/数字/下划线/连字符/点/冒号（非法字符：{bad}）。"
            "中文、空白与斜杠都会让 Redis 的 key 形状不可预测"
        )
    return text


def derive_key(*, function: str, user_id: int | None, params: dict[str, Any] | None) -> str:
    """从业务字段派生幂等键（调用方没给 key 时的兜底）。

    形如 `report:u2:3f2a1c9d8b7e`：**前缀可读**（排障时一眼看出是谁的什么活），
    **尾部是摘要**（参数可能是任意 JSON，不能直接进 key）。

    摘要用 `sha256` 前 24 位十六进制（96 bit）：碰撞概率低于「同一个人同一秒内
    发出两个参数不同的同名报表」这件事本身。规范 JSON（`sort_keys` + 紧凑分隔符）
    是必须的 —— 否则键序不同就会派生两个 key（见模块 docstring）。
    """
    material = json.dumps(params or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{function}\x00{user_id}\x00{material}".encode()).hexdigest()[:24]
    owner = "anon" if user_id is None else f"u{int(user_id)}"
    return normalize_key(f"{kind_of_function(function)}:{owner}:{digest}")


def kind_of_function(function: str) -> str:
    """任务名 → 种类（反向查表；未知时返回 `"task"`，用于日志前缀而不是抛错）。"""
    for kind, name in KIND_TO_FUNCTION.items():
        if name == function:
            return kind
    return "task"


def job_id_for(idempotency_key: str) -> str:
    """幂等键 → `job_id`（稳定、可读、只含安全字符）。

    ⚠️ 这里**不能**复用 `normalize_key` 做校验（本仓实测踩到）：
    `MAX_KEY_CHARS = 64` 是**幂等键**的上限，而 `job_id` 还要多一个 `q_` 前缀 ——
    直接把带前缀的结果再喂给 `normalize_key`，会让「64 字符的键」在最后一步
    抛 `幂等键过长（66 > 64）`。症状是「一个刚好卡在边界上的幂等键永远入不了队」，
    而且报错指向的是键而不是 `job_id`（回看时很容易以为是调用方写错了）。
    所以校验分两步：**先按键的规则校验键，再按 `job_id` 的规则校验 `job_id`**。
    """
    key = normalize_key(idempotency_key)
    job_id = f"{JOB_ID_PREFIX}{key}"
    if len(job_id) > MAX_KEY_CHARS + len(JOB_ID_PREFIX):
        raise ValueError(f"job_id 过长（{len(job_id)}）：幂等键必须 <= {MAX_KEY_CHARS} 字符")
    return job_id


def policy_summary() -> dict[str, dict[str, Any]]:
    """策略表的结构化快照（验收命令与文档用它，避免各处手打数字）。"""
    return {
        function: {
            "attempts": policy.attempts,
            "base_delay": policy.base_delay,
            "max_delay": policy.max_delay,
            "jitter": policy.jitter,
            "delays": [round(policy.delay_for(n), 4) for n in range(1, policy.attempts)],
        }
        for function, policy in DEFAULT_POLICIES.items()
    }


__all__ = [
    "DEFAULT_POLICIES",
    "FUNC_AGENT",
    "FUNC_KB_REBUILD",
    "FUNC_REPORT",
    "FUNCTIONS",
    "JobKind",
    "JobSpec",
    "JOB_ID_PREFIX",
    "JOB_KINDS",
    "JOB_TIMEOUT_SECONDS",
    "KIND_TO_FUNCTION",
    "MAX_DEFER_SECONDS",
    "MAX_KEY_CHARS",
    "RESULT_TTL_SECONDS",
    "RetryPolicy",
    "derive_key",
    "job_id_for",
    "kind_of_function",
    "normalize_key",
    "policy_for",
    "policy_summary",
]
