"""本模块的**结构契约**：分层、登记表、参数口径、与既有模块的对账。

这些用例的价值不在「逻辑对不对」（那些在别的文件里），而在**防止漂移**：
队列这一层有六处「两个地方各写一遍」的诱惑（任务名、种类、策略、
worker 注册表、限流三道登记、DB 的 kind 白名单），任何一处漂移都不会报错，
只会在某个更远的时刻变成一次难查的故障。

⚠️ 分层检查（`test_queue_layer_does_not_import_the_web_or_graph_layers`）
用的是**源码扫描**而不是「跑一下试试」：`app/queue` 一旦 import 了 FastAPI，
`uv run arq app.queue.worker.WorkerSettings` 这个独立进程就会连带拉起整个 Web 栈
（第 8 步的图就是这么被拉起来的，所以那条边界单独钉）。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.db.models import INFRA_MODELS, QUEUE_JOB_KINDS, QueueDeadLetter
from app.queue import jobs as jobs_module
from app.queue.jobs import (
    DEFAULT_POLICIES,
    FUNC_AGENT,
    FUNC_KB_REBUILD,
    FUNC_REPORT,
    FUNCTIONS,
    JOB_KINDS,
    JOB_TIMEOUT_SECONDS,
    KIND_TO_FUNCTION,
    RESULT_TTL_SECONDS,
    RetryPolicy,
    derive_key,
    job_id_for,
    normalize_key,
    policy_for,
)
from app.queue.tasks import DISPATCH

QUEUE_DIR = Path(__file__).resolve().parents[2] / "app" / "queue"


def _sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in QUEUE_DIR.glob("*.py")}


def _imports(name: str) -> list[str]:
    """取出一个模块的**真实 import 行**（跳过 docstring 与注释里的示例）。

    ⚠️ 为什么不能直接在源码里 `in` 一个包名：本仓的 docstring 里到处是
    `from app.graph import ...` 这样的**示例**（那是有意写给读者看的），
    拿它们当违例会把「文档写得好」判成「分层坏了」。
    判定 `import`/`from` 开头且**不在三引号块内**，就足够精确。
    """
    lines = _sources()[name].splitlines()
    out: list[str] = []
    in_docstring = False
    for line in lines:
        stripped = line.strip()
        if stripped.count('"""') % 2 == 1:
            in_docstring = not in_docstring
            continue
        if in_docstring or stripped.startswith("#"):
            continue
        if stripped.startswith(("import ", "from ")):
            out.append(stripped)
    return out


# ── 分层 ────────────────────────────────────────────────────────────────────


def test_transport_layer_does_not_import_business_layers():
    """`backend.py`（传输层）不许 import 图 / Agent / 检索。

    `agent_runs.py` **故意允许**（它的职责就是「把三个业务动作包成任务」），
    而 `core.py` / `backend.py` / `jobs.py` 是**通用队列设施** ——
    它们一旦依赖业务模块，队列就再也不能被别的业务复用，
    而「换传输层不改策略」这条结构决定也跟着失效。
    """
    forbidden = ("fastapi", "starlette", "app.api", "app.graph", "app.agents", "app.rag", "litellm", "openai")
    offenders: list[str] = []
    for name in ("core.py", "backend.py", "jobs.py", "worker.py", "store.py", "__init__.py"):
        for line in _imports(name):
            for token in forbidden:
                if re.search(rf"\b{re.escape(token)}\b", line):
                    # worker.py 允许 import 任务实现（`from app.queue import agent_runs`）
                    offenders.append(f"{name}: {line}")
    assert not offenders, "通用队列设施里出现了越界 import：\n" + "\n".join(offenders)


def test_queue_never_imports_the_web_layer():
    """整包**任何**模块都不许 import FastAPI（接线方向只有一个：`app/api` → `app.queue`）。"""
    offenders = [
        f"{name}: {line}"
        for name in _sources()
        for line in _imports(name)
        if re.search(r"\b(fastapi|starlette)\b", line)
    ]
    assert not offenders, "\n".join(offenders)


def test_store_is_the_only_module_that_touches_the_orm():
    """死信表只由 `store.py` 碰（其余模块用 `DeadLetterStore` 协议）。

    这条边界让「内存实现能在离线用例里替掉真库」这件事成立
    （如果 `core.py` 直接 import 模型，离线用例就必须连库）。
    """
    offenders = [
        f"{name}: {line}"
        for name in _sources()
        if name != "store.py"
        for line in _imports(name)
        if line.startswith(("from app.db.models", "from app.db.session", "import app.db"))
    ]
    assert not offenders, "\n".join(offenders)


def test_core_does_not_import_arq():
    """内核（`core.py`）**不许**知道传输层是谁 —— 这是「队列语义可离线验证」的前提。"""
    offenders = [line for line in _imports("core.py") if re.search(r"\barq\b", line)]
    assert not offenders, "\n".join(offenders)


# ── 登记表之间的一致性 ───────────────────────────────────────────────────────


def test_db_kind_whitelist_matches_the_job_kinds():
    """DB 的 `kind` CHECK 约束与 `jobs.JOB_KINDS` 必须逐字一致。

    漂移的症状：新增一种任务时**入队成功、落库失败**（CheckViolation），
    而那一刻任务已经跑完了 —— 失败现场写不进去，等于「静默丢任务」。
    """
    assert tuple(QUEUE_JOB_KINDS) == tuple(JOB_KINDS)
    constraint = next(
        item for item in QueueDeadLetter.__table__.constraints if item.name == "ck_queue_dead_letters_kind_valid"
    )
    for kind in JOB_KINDS:
        assert f"'{kind}'" in str(constraint.sqltext)


def test_task_names_kinds_and_dispatch_line_up():
    """任务名 / 种类 / 分派表三处必须一一对应（新增任务时最容易漏一处）。"""
    assert set(KIND_TO_FUNCTION) == set(JOB_KINDS)
    assert set(KIND_TO_FUNCTION.values()) == set(FUNCTIONS)
    assert set(DISPATCH) == set(JOB_KINDS)
    # 分派表里的名字必须真的存在（`dispatch_enqueue` 用 globals()[name] 找它）
    from app.queue import tasks as tasks_module

    for kind, name in DISPATCH.items():
        assert callable(getattr(tasks_module, name, None)), f"{kind} 指向了不存在的 {name}"


def test_every_kind_has_a_policy_and_a_timeout():
    """三种任务都必须有策略与超时（缺策略 = 静默地只跑一次；缺超时 = 永久占住 worker 槽位）。"""
    for function in FUNCTIONS:
        policy = policy_for(function)
        assert isinstance(policy, RetryPolicy)
        assert policy.attempts >= 1
    assert JOB_TIMEOUT_SECONDS > 0
    assert RESULT_TTL_SECONDS > 0


def test_infra_models_are_separate_from_business_models():
    """死信表属于**基础设施表**，不该混进「8 张业务表」那条口径。"""
    from app.db.models import BUSINESS_MODELS

    assert len(BUSINESS_MODELS) == 8
    assert QueueDeadLetter in INFRA_MODELS
    assert QueueDeadLetter not in BUSINESS_MODELS


def test_db_check_knows_about_the_infra_table():
    """`scripts/db_check` 的必需表清单要包含死信表（否则「迁移跑没跑」看不出来）。"""
    from scripts.db_check import BUSINESS_TABLES, INFRA_TABLES, REQUIRED_TABLES

    assert "queue_dead_letters" in INFRA_TABLES
    assert "queue_dead_letters" in REQUIRED_TABLES
    assert "queue_dead_letters" not in BUSINESS_TABLES
    assert len(BUSINESS_TABLES) == 8


def test_worker_settings_register_every_task():
    """worker 的注册表覆盖全部任务名（漏一个 = 入队成功、执行时 KeyError）。"""
    from app.queue.worker import WorkerSettings, registered_functions

    assert sorted(item.name for item in WorkerSettings.functions) == sorted(FUNCTIONS)
    assert sorted(item.name for item in registered_functions()) == sorted(FUNCTIONS)


def test_facade_exports_are_real_and_documented():
    """门面出口都真的存在（打错一个名字只会在别人 import 时炸）。"""
    import app.queue as facade

    for name in facade.__all__:
        assert hasattr(facade, name), name
        assert getattr(facade, name) is not None or name.endswith("__"), name
    # 文档里的两行用法必须在门面里拿得到
    assert callable(facade.enqueue_report)
    assert callable(facade.enqueue_agent_run)
    assert facade.WorkerSettings is not None


# ── 参数口径 ─────────────────────────────────────────────────────────────────


def test_job_timeout_is_smaller_than_the_result_ttl():
    """超时必须**小于**结果保留时长：否则「超时」那一刻结果已经过期，
    客户端永远拿不到「为什么没跑出来」的答案。"""
    assert JOB_TIMEOUT_SECONDS < RESULT_TTL_SECONDS


def test_max_defer_is_bounded_by_the_expiry_rule():
    """延迟上限必须是个有限值（否则任务可能在 Redis 里躺到没人记得）。"""
    assert jobs_module.MAX_DEFER_SECONDS <= 90 * 24 * 3600.0


def test_derive_key_prefix_matches_a_registered_kind():
    """派生键的前缀必须是登记过的种类（`report:` / `agent:` / `kb_rebuild:`）。"""
    for function in FUNCTIONS:
        key = derive_key(function=function, user_id=1, params={})
        assert key.split(":", 1)[0] == jobs_module.kind_of_function(function)


def test_job_id_and_key_are_length_bounded():
    """`job_id` 必须能进 DB 的 `job_id` 列（`String(96)`）—— 超了会在**落库时**才炸。

    ⚠️ 边界值（`MAX_KEY_CHARS` 字符的键）在这里**必须**成功：`job_id = 'q_' + 键`，
    所以「键的最大长度」与「job_id 的最大长度」不是同一个数 ——
    第一版把带前缀的结果又喂回 `normalize_key`，于是「刚好 64 字符的键」
    在最后一步抛 `幂等键过长（66 > 64）`（见 runbook §10.1）。
    """
    assert QueueDeadLetter.__table__.c.job_id.type.length == 96
    boundary = job_id_for("x" * jobs_module.MAX_KEY_CHARS)
    assert boundary == f"q_{'x' * jobs_module.MAX_KEY_CHARS}"
    assert len(boundary) <= 96
    # 超过键的上限仍然要拒（否则 DB 列会被撑爆）
    with pytest.raises(ValueError):
        job_id_for("y" * (jobs_module.MAX_KEY_CHARS + 1))


def test_build_spec_rejects_bad_input_before_enqueueing():
    """入参自检在**入队前**（而不是等 worker 跑一趟再进死信）。"""
    from app.queue.tasks import build_spec

    with pytest.raises(ValueError):
        build_spec(function=FUNC_AGENT, kind="agent", payload={"message": "你好"}, user_id=None)
    with pytest.raises(ValueError):
        build_spec(function=FUNC_REPORT, kind="report", payload={"user_id": 2}, defer_by=-1)
    with pytest.raises(ValueError):
        build_spec(function=FUNC_REPORT, kind="report", payload={"user_id": 2}, defer_by=10**9)
    with pytest.raises(ValueError):
        build_spec(function=FUNC_KB_REBUILD, kind="kb_rebuild", payload={}, idempotency_key="a b")


def test_build_spec_derives_a_stable_key():
    """两次组装同一个请求 → 同一个 `job_id`（幂等的**实现**就在这一行）。"""
    from app.queue.tasks import build_spec

    first = build_spec(function=FUNC_REPORT, kind="report", payload={"user_id": 2, "title": "x"}, user_id=2)
    second = build_spec(function=FUNC_REPORT, kind="report", payload={"title": "x", "user_id": 2}, user_id=2)
    assert first.job_id == second.job_id
    assert first.idempotency_key == second.idempotency_key


# ── 与既有模块的对账 ─────────────────────────────────────────────────────────


def test_queue_does_not_duplicate_the_retry_classification():
    """分类只**归并**既有白名单（不重新发明一套）—— 源码级检查。

    这条防的是「队列里又写一份 if isinstance(...) 列表」：
    那种重复在新增异常类型时会静默漂移（队列与工具层对同一个错误给出不同判断）。
    """
    source = _sources()["core.py"]
    assert "from app.tools.errors import" in source
    assert "from app.llm.errors import" in source
    # 允许出现类型名（在 classify 的分支里），但不允许再列一遍白名单常量
    assert "RETRYABLE_SQLSTATES" not in source


def test_run_turn_exceptions_are_not_swallowed_by_the_queue():
    """第 8 步刻意上抛的 `RetryableToolError`，在第 12 步必须被**当成可重试**（而不是吞掉）。

    这是两个模块之间唯一一条口头约定（`8.graph-runbook.md` §5.4 写着
    「本步**刻意不兜**它们」），所以它必须有自动化版本，否则将来谁在
    `run_turn` 里加一个 `except RetryableToolError` + 兜底，就没人发现
    —— 而那一天「该退避的错误」会被悄悄降级成「查不到」。
    """
    from app.graph.run import run_turn

    source = inspect.getsource(run_turn)
    assert "except RetryableToolError:" in source
    # `except RetryableToolError:` 之后的第一条可执行语句必须是 `raise`
    tail = source.split("except RetryableToolError:", 1)[1]
    body = [line.strip() for line in tail.splitlines()[1:] if line.strip() and not line.strip().startswith("#")]
    assert body and body[0].startswith("raise"), f"`except RetryableToolError` 里第一句是 {body[:1]}"

    from app.queue.core import classify
    from app.tools.errors import RetryableToolError

    assert classify(RetryableToolError("x")) is True


def test_agent_task_turns_exhausted_into_a_retryable_error():
    """`agent` 任务把 `Turn.exhausted` 翻成可重试错误 —— 源码级钉住那条翻译。

    少了它，「重放修不好的失败」会静默地变成「成功的任务」，而死信队列永远是空的。
    """
    from app.queue.agent_runs import run_agent_turn

    source = inspect.getsource(run_agent_turn)
    assert "turn.exhausted" in source
    assert "RetryableToolError" in source


@pytest.mark.parametrize(
    "function",
    [FUNC_REPORT, FUNC_AGENT, FUNC_KB_REBUILD],
)
def test_runners_are_coroutines(function: str) -> None:
    """任务实现必须是协程函数（arq 只接受协程；写错的话在 worker 里才炸）。"""
    from app.queue.worker import runner_for

    assert inspect.iscoroutinefunction(runner_for(function))


def test_default_policies_are_jittered_in_production():
    """生产策略**都要带抖动**：一批同时失败的任务不该同时回来（惊群）。"""
    for function, policy in DEFAULT_POLICIES.items():
        assert policy.jitter > 0, function
