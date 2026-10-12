"""任务契约与重试策略的纯逻辑用例（**不碰队列、不碰 Redis、不碰模型**）。

覆盖 `app/queue/jobs.py` 的三件事：**幂等键、重试策略、任务名**。
它们值得单独一个文件，因为这三件事的错误都**不会报错**：

* 幂等键不稳定 → 同一个请求跑两遍（只是账单变贵、副作用重复）；
* 退避算错 → 任务以固定间隔撞下游（只是下游更慢）；
* 任务名改了 → 队列里的老任务找不到函数（只在升级发布的那一刻暴露）。
"""

from __future__ import annotations

import pytest

from app.queue.jobs import (
    DEFAULT_POLICIES,
    FUNC_AGENT,
    FUNC_KB_REBUILD,
    FUNC_REPORT,
    FUNCTIONS,
    JOB_ID_PREFIX,
    KIND_TO_FUNCTION,
    MAX_DEFER_SECONDS,
    MAX_KEY_CHARS,
    RetryPolicy,
    derive_key,
    job_id_for,
    kind_of_function,
    normalize_key,
    policy_for,
    policy_summary,
)


# ── 幂等键 ───────────────────────────────────────────────────────────────────


def test_derive_key_is_a_pure_function_of_the_business_request():
    """同一份业务参数派生同一个键；**参数字典的键序不影响结果**。

    第二条是本文件最值钱的一条：`{"a":1,"b":2}` 与 `{"b":2,"a":1}` 是同一个请求，
    而 Python 的 `json.dumps` 默认**保持插入顺序** —— 不规范化就会派生出两个键，
    于是「重投一次」变成「跑两个任务」，而调用方完全看不出区别。
    """
    first = derive_key(function=FUNC_REPORT, user_id=2, params={"title": "本月", "row_limit": 10})
    assert first == derive_key(function=FUNC_REPORT, user_id=2, params={"row_limit": 10, "title": "本月"})
    # 换 user_id / 换参数 / 换任务名 → 不同的键（三者都是幂等语义的一部分）
    assert first != derive_key(function=FUNC_REPORT, user_id=3, params={"title": "本月", "row_limit": 10})
    assert first != derive_key(function=FUNC_REPORT, user_id=2, params={"title": "上月", "row_limit": 10})
    assert first != derive_key(function=FUNC_KB_REBUILD, user_id=2, params={"title": "本月", "row_limit": 10})


def test_derive_key_is_readable_and_prefixed_by_kind():
    """派生键的形状是 `<kind>:u<id>:<摘要>`（排障时一眼看出是谁的什么活）。"""
    key = derive_key(function=FUNC_REPORT, user_id=7, params={"title": "x"})
    assert key.startswith("report:u7:")
    assert len(key) <= MAX_KEY_CHARS
    assert kind_of_function(FUNC_REPORT) == "report"


def test_derive_key_handles_missing_user():
    """没有 user_id 时用 `anon`（知识库重建就是这种任务）。"""
    assert derive_key(function=FUNC_KB_REBUILD, user_id=None, params={}).startswith("kb_rebuild:anon:")


def test_derive_key_never_uses_randomness():
    """20 次派生必须完全一致 —— 用随机数/时间戳就等于是「每次都算新请求」。"""
    keys = {derive_key(function=FUNC_AGENT, user_id=2, params={"message": "你好"}) for _ in range(20)}
    assert len(keys) == 1


def test_normalize_key_rejects_instead_of_silently_cleaning():
    """非法字符**抛错**，不做静默清洗。

    静默清洗的两种后果都很难查：`"a b"` 与 `"ab"` 会被清成同一个键（两个请求撞成一个），
    而含空格的键进 Redis key 会让 `keys` 出来的东西看不懂。
    """
    for bad in ("", "   ", "a b", "a/b", "键", "a" * (MAX_KEY_CHARS + 1)):
        with pytest.raises(ValueError):
            normalize_key(bad)
    assert normalize_key("  chat:u2:abc-1.2  ") == "chat:u2:abc-1.2"


def test_job_id_is_prefixed_and_safe():
    job_id = job_id_for("report:u2:deadbeef")
    assert job_id == f"{JOB_ID_PREFIX}report:u2:deadbeef"
    assert job_id_for("report:u2:deadbeef") == job_id  # 稳定


# ── 重试策略 ─────────────────────────────────────────────────────────────────


def test_retry_policy_is_exponential_and_capped():
    """退避是指数的、且被 `max_delay` 封顶。"""
    policy = RetryPolicy.jitless(attempts=6, base_delay=1.0, max_delay=4.0)
    assert [policy.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 4.0, 4.0]
    # 超过额度时返回 0（返回一个大数会让「判定写反」变成一次「等了五分钟才发现」的排障）
    assert policy.delay_for(6) == 0.0


def test_should_retry_counts_attempts_correctly():
    """`attempts=3` 意味着「首次 + 两次重试」——**不是**「重试三次」。"""
    policy = RetryPolicy.jitless(attempts=3)
    assert [policy.should_retry(n) for n in (1, 2, 3, 4)] == [True, True, False, False]


def test_jitter_stays_within_bounds_and_never_negative():
    """抖动是对称的、非负的（`jitter=1.0` 的下界是 0，不能变成负数）。

    ⚠️ 负的 `defer` 会让 arq 把任务当成「早就该跑」→ 立刻执行。看起来没事，
    实际是「退避配置写错」被静默地吃掉了。
    """
    policy = RetryPolicy(attempts=3, base_delay=10.0, max_delay=100.0, jitter=1.0)
    for _ in range(50):
        value = policy.delay_for(1)
        assert 0.0 <= value <= 20.0


def test_retry_policy_rejects_nonsense():
    """三个非法组合在**构造期**就报错（而不是在第一次失败时才表现异常）。"""
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=10.0, max_delay=1.0)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=1.5)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=-1.0)


def test_policy_for_unknown_function_is_the_most_conservative():
    """未登记策略的任务**只有一次机会**（而不是默认 3 次）。

    这是刻意的：没登记策略的任务多半是新加的、还没想过「失败了怎么办」——
    对它重试等于用真实流量做实验。不重试会让它立刻在死信队列里露出来。
    """
    policy = policy_for("shopsage.something_new")
    assert policy.attempts == 1
    assert policy.should_retry(1) is False


def test_every_registered_function_has_a_policy():
    """任务名与策略表必须一一对应（worker 的 `selfcheck()` 依赖它）。"""
    assert set(DEFAULT_POLICIES) == set(FUNCTIONS)
    assert set(KIND_TO_FUNCTION.values()) == set(FUNCTIONS)


def test_kb_rebuild_gets_the_most_retries():
    """知识库重建的额度最大（幂等 + 贵 → 宁可多等几轮，也别落到人工队列）。"""
    assert DEFAULT_POLICIES[FUNC_KB_REBUILD].attempts > DEFAULT_POLICIES[FUNC_REPORT].attempts
    assert DEFAULT_POLICIES[FUNC_KB_REBUILD].attempts > DEFAULT_POLICIES[FUNC_AGENT].attempts


def test_policy_summary_is_a_plain_readable_snapshot():
    """验收命令与文档用 `policy_summary()` 取数字，避免手抄（手抄必然漂移）。"""
    summary = policy_summary()
    assert set(summary) == set(FUNCTIONS)
    for name, item in summary.items():
        assert item["attempts"] == DEFAULT_POLICIES[name].attempts
        # delays 的长度 = attempts - 1（最后一次失败之后不再重试，所以没有延迟）
        assert len(item["delays"]) == item["attempts"] - 1


def test_max_defer_is_a_month():
    """延迟任务的提前量上限是 30 天（更远的定时任务该用 cron）。"""
    assert MAX_DEFER_SECONDS == 30 * 24 * 3600.0
