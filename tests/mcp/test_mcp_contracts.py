"""MCP 契约测试：把 `15.mcp.md` 的四条设计决策变成**可执行的检查**。

风格与 `tests/test_tools_contracts.py` 一致：用 `inspect` / AST 遍历**真实代码**，
而不是再抄一份清单 —— 抄出来的清单一改就过期，遍历真实对象则「新增工具忘了写审计」
「顺手在 MCP 里拼了 SQL」会立刻红。

| 契约 | 对应文档 | 忘了会怎样 |
|---|---|---|
| MCP 工具与 `6.tools.md` 一一对应，无重复实现 | §4 第 1 条 | 两套实现两套校验，安全策略迟早漂移 |
| 工具名就是 `app/tools/` 里的 `TOOL_NAME` | §2 + 6.tools §3.1 | 审计按工具名归并，MCP 调用会变成另一个名字、统计分裂 |
| 身份参数**不在**工具签名里 | §4 第 2 条（D1） | 客户端申报身份 = 行级过滤形同虚设 |
| `app/mcp/` 里没有写库/建会话/拼 SQL 的代码 | §4 第 1、3 条 | 「薄壳」被慢慢写成第二个 tools 层 |
| 内部敏感能力不暴露 | §4 第 3 条 | 退款 / 越权读被外部客户端直接调用 |
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import app.mcp as mcp_pkg
from app.mcp import server as server_mod
from app.mcp import sync_calls, tools as tool_impl
from app.mcp.settings import EXPOSED_TOOLS, McpSettings, TOOL_DESCRIPTIONS
from app.security.policy import ROW_FILTER_COLUMN

MCP_DIR = Path(mcp_pkg.__file__).parent

#: 逐个工具的实现落点（`app/tools/` 的哪个函数）。
#: ⚠️ 这张表**只用来断言「真的是同一个函数」**（对象身份），不是清单的副本：
#: 多一个工具就会让 `EXPOSED_TOOLS` 的对账失败，漏改表也一样。
TOOL_IMPL = {
    "sql_query": ("app.tools.sql_executor", "execute_readonly_query"),
    "retrieve_knowledge": ("app.tools.retriever", "retrieve_docs"),
    "create_ticket": ("app.tools.ticket", "create_ticket"),
    "build_chart": ("app.tools.chart", "build_chart_spec"),
}

#: `app/mcp/` 里**不允许出现**的可执行标识（写路径/建会话/自己解析 SQL）。
#: 只查 AST 里的代码名，不查字符串 —— 模块 docstring 里为了解释「为什么调工具层」
#: 会自然地提到 `INSERT ... ON CONFLICT`，把文档也算进来会逼着人删掉说明。
FORBIDDEN_NAMES = {
    "SessionLocal",
    "get_write_engine",
    "write_session",
    "insert_once",
}


def _code_names(path: Path) -> set[str]:
    """一个模块里出现的所有可执行标识（函数/属性/match 到的名字），**排除 docstring**。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                names.add(node.value)
    return names


def test_exposure_list_is_exactly_the_documented_four():
    """`15.mcp.md` §2 的四个工具，**不多不少**（第 4 节第 3 条：只暴露审计过的子集）。"""
    assert EXPOSED_TOOLS == ("sql_query", "retrieve_knowledge", "create_ticket", "build_chart")
    assert set(TOOL_DESCRIPTIONS) == set(EXPOSED_TOOLS), "每个对外工具都必须有描述（客户端靠它发现能力）"


@pytest.mark.parametrize(
    ("name", "reference", "argument"),
    [
        ("sql_query", "execute_readonly_query", "SELECT order_id FROM orders WHERE user_id = :user_id LIMIT 1"),
        ("retrieve_knowledge", "retrieve_docs", "退货要多久"),
        ("build_chart", "build_chart_spec", [{"k": "a", "v": 1}]),
    ],
)
async def test_mcp_tools_call_the_tool_layer_function(
    name, reference, argument, customer_settings, monkeypatch
):
    """★薄壳的机械证明：调一次 MCP 工具，被调用的**确实是工具层那个函数**。

    `sql_query` 是最容易看出价值的那个：它换掉的是 `execute_readonly_query`，
    于是本用例能同时钉住「边界就在这一层」—— 那条 SQL **不会**真的执行
    （假函数被调用即返回），而 `caller` 已经变成 `mcp:sql_query`（审计字段可见）。
    """
    calls: list[tuple[tuple, dict]] = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if reference == "retrieve_docs":
            return []
        if reference == "build_chart_spec":
            return {"schema": "shopsage.chart/v1", "data": []}
        return [], {
            "audit_id": "tc-fake",
            "row_count": 0,
            "truncated": False,
            "columns": [],
            "tables": ["orders"],
            "declared_limit": 1,
            "limit_clamped": False,
            "bound_limit_clamped": False,
            "rows_cap": 100,
            "duration_ms": 0,
        }

    monkeypatch.setattr(sync_calls, reference, fake)

    fn = getattr(tool_impl, name)
    if name == "sql_query":
        await fn(argument, {"user_id": 1}, None, settings=customer_settings)
    elif name == "retrieve_knowledge":
        await fn(argument, 3, settings=customer_settings)
    else:
        await fn(argument, "k", "v", settings=customer_settings)

    assert calls, f"{name} 没有调用工具层的 {reference}（薄壳里出现了第二套实现）"
    # `caller` 由入口盖章（`caller_override`），所以薄壳传给工具层的是 None。
    # 审计里最终看到的是 `mcp:<工具名>` —— 那一条在 test_mcp_audit.py 里用真调用验证。
    assert calls[0][1].get("caller") is None, "入口盖章时应传 caller=None，让工具内部读 caller_override"


def test_thin_shell_holds_the_same_function_objects():
    """薄壳手里的引用**就是**工具层那些函数对象（不是同名包装）。

    与上面那条「被调用的是谁」互补：这条是静态的、零副作用的对账，
    上面那条是动态的（真的调一次）。两条一起才能排除「引用对了但调用时换了一个」。
    """
    for tool_name, (module_name, attr) in TOOL_IMPL.items():
        target = getattr(__import__(module_name, fromlist=[attr]), attr)
        holder = {
            "sql_query": "execute_readonly_query",
            "retrieve_knowledge": "retrieve_docs",
            "create_ticket": "create_ticket_tool",
            "build_chart": "build_chart_spec",
        }[tool_name]
        assert getattr(sync_calls, holder) is target, f"{tool_name} 没有复用工具层的 {attr}"


def test_tool_names_match_the_tool_layer_audit_names():
    """MCP 的工具名 == 工具层的 `TOOL_NAME`（审计按工具名归并，名字必须一致）。"""
    from app.tools.chart import TOOL_NAME as CHART_TOOL
    from app.tools.retriever import TOOL_NAME as RETRIEVE_TOOL
    from app.tools.sql_executor import TOOL_NAME as SQL_TOOL

    assert tool_impl.SQL_QUERY == SQL_TOOL == "sql_query"
    assert tool_impl.RETRIEVE_KNOWLEDGE == RETRIEVE_TOOL == "retrieve_knowledge"
    assert tool_impl.BUILD_CHART == CHART_TOOL == "build_chart"
    # 工单：`app/tools/ticket.py` 的 `CREATE_TOOL` 与 MCP 名字一致
    from app.tools.ticket import CREATE_TOOL

    assert tool_impl.CREATE_TICKET == CREATE_TOOL == "create_ticket"


@pytest.mark.parametrize("name", EXPOSED_TOOLS)
def test_no_identity_can_be_declared_by_the_client(name):
    """★D1：**任何工具签名里都不许出现 `role`**（身份/权限不是客户端参数）。

    这一条是本模块最值钱的契约。`role` 一旦成为参数，客户端就能把自己说成
    `customer_service` / `admin`；`requester_user_id` 一旦成为参数，行级过滤就变成
    「你说是谁就是谁」。身份只能来自服务端配置（`McpSettings`）。
    """
    for fn in (getattr(tool_impl, name), getattr(server_mod, "_" + name, None)):
        if fn is None:
            continue
        params = set(inspect.signature(fn).parameters)
        assert "role" not in params, f"{name} 暴露了 role 参数：身份不能由客户端申报"
        assert "requester_user_id" not in params, f"{name} 暴露了 requester_user_id"


def test_sql_query_requires_the_row_filter_binding_parameter():
    """只读策略下，SQL 里必须有 `user_id = :user_id`（参数名而不是值，值另有校验）。"""
    assert sync_calls.FILTER_PARAM == ROW_FILTER_COLUMN == "user_id"
    # 三个反例都必须被拦（正例见 test_mcp_security.py）
    for bad in (
        "SELECT order_id FROM orders WHERE status = :s",
        "SELECT order_id FROM orders WHERE user_id = 1",
        "SELECT order_id FROM orders WHERE user_id = :other_user",
    ):
        with pytest.raises(Exception):
            sync_calls.assert_customer_row_filter(bad)


def test_mcp_package_touches_no_write_path_and_no_own_sql():
    """`app/mcp/` 是薄壳：不许建写会话、不许拿写引擎、不许自己解析 SQL。

    用 AST 而不是 grep（见 `_code_names`）：文档里解释「为什么要调工具层」会提到
    写语句，那是说明而不是实现。
    """
    offenders: list[str] = []
    for path in sorted(MCP_DIR.glob("*.py")):
        names = _code_names(path)
        hit = FORBIDDEN_NAMES & names
        if hit:
            offenders.append(f"{path.name}: {sorted(hit)}")
    assert not offenders, "MCP 层碰了写路径（薄壳不许有第二套写库实现）：" + "; ".join(offenders)


def test_settings_are_required_keyword_on_every_async_tool():
    """每个协议薄壳都必须有 keyword-only 的 `settings`（身份是必填，不是可选装饰）。"""
    for name in EXPOSED_TOOLS:
        fn = getattr(tool_impl, name)
        assert inspect.iscoroutinefunction(fn), f"{name} 必须是 async（见 tools.py 的说明）"
        parameter = inspect.signature(fn).parameters.get("settings")
        assert parameter is not None, f"{name} 缺 settings 参数"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_internal_sensitive_tools_are_not_exposed():
    """内部敏感能力不对外：退款、查工单/退款状态、原始 `retrieve_docs` 注入点都不在清单里。"""
    for name in (
        "create_refund_request",
        "get_refund_status",
        "get_ticket_status",
        "create_refund",
        "execute_sql",
        "run_sql",
    ):
        assert name not in EXPOSED_TOOLS, f"{name} 不该出现在对外清单里"


def test_customer_policy_is_the_default():
    """默认策略必须是更严的那一档：默认值选错了，配错的方向就是「越权」。"""
    assert McpSettings().policy == "customer"
    assert McpSettings().is_customer_scoped is True
    # 对客策略缺身份 → 直接不可用（在启动之前就拦住）
    with pytest.raises(ValueError, match="MCP_ACTOR_USER_ID"):
        McpSettings(policy="customer").identity()


def test_unknown_policy_is_rejected_not_defaulted():
    """策略名拼错必须报错，而不是静默退化成 internal（那是最宽的一档）。"""
    problems = McpSettings(policy="custmer", actor_user_id=1).validate()
    assert any("policy 必须是" in item for item in problems)


def test_mcp_package_does_not_import_the_web_layer():
    """`app/mcp` 不 import web 层：MCP 是一个可独立部署的进程（同 `app.eval` 的纪律）。

    ⚠️ 判据必须看 **import 语句**，不能看 `sys.modules`（第一版就是这么写错的）：
    跑全量时 `fastapi` 早被 `tests/api/` 拖进来了，于是
    「import app.mcp 之后 sys.modules 里有没有 fastapi」**永远为真** ——
    用例的失败原因与它名字里的东西无关（正是 §7.3 那条纪律要挡的情况）。

    反面教材（本步实测，runbook §10.1 坑 2）：`app/mcp/settings.py` 第一版
    `from app.api.config import _env_value`，把 `app.api` 整条链（含 uvicorn）拖了进来。
    本用例就是为它写的。
    """
    forbidden = ("app.api", "fastapi", "uvicorn", "starlette")
    offenders: list[str] = []
    for path in sorted(MCP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if any(module == bad or module.startswith(bad + ".") for bad in forbidden):
                    offenders.append(f"{path.name}: import {module}")
    assert not offenders, "MCP 层拖了 web 层进来（薄壳应当能独立部署）：" + "; ".join(offenders)
