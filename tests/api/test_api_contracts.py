"""契约用例：**API 层的边界**（`9.api.md` §4 第 4 条「API 层无业务逻辑」的自动化版）。

第 8 步用 AST 检查钉住了「图层不许碰库、不许直连模型」（`tests/graph/test_graph_contracts.py`）。
本文件做同一件事，但对象换成 API 层 —— 原因是同一条：**越界不会报错，只会让架构慢慢腐化**。
一个路由里写一句 `SELECT ...` 也能跑、也能过功能用例，但它把「行级过滤只有一处真相」
（`3.db-runbook.md` §5.3 的决策）变成了「两处」，而第二处不会有人去审计。

四条契约：

| 契约 | 违反后的症状 |
|---|---|
| 不许出现 SQL / ORM / 模型 SDK | 业务逻辑回流到路由；行级过滤出现第二处真相 |
| 不许出现 prompt 文本 | 改 prompt 要动 API 层；六个 Agent 的独立性被打破 |
| 不许出现脱敏规则 | 脱敏出现第二处真相（`app/security/masking.py` 是第一处） |
| 不许出现 `row_filter` 生成逻辑 | D1 的过滤可能被绕过（API 层不该"顺手"拼 WHERE） |

⚠️ 允许出现的例外都写在 `ALLOWED_SQL_TOKENS` 里（目前只有「注释里提到 SQL 这个词」——
本仓的注释很爱解释「这里为什么不查库」，所以不允许注释里出现 `SELECT` 会过度收紧）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[2] / "app" / "api"

#: 禁止出现的**调用/属性**名（按「一旦出现就说明越界」挑，不做正则全文匹配）
FORBIDDEN_IMPORTS = (
    "litellm",
    "openai",
    "sqlalchemy",
    "sqlglot",
    "psycopg",
)

#: 禁止出现的调用名（本包内任何 `.py` 的文件里都不该出现）
#:
#: ⚠️ **变更 001 把 `SessionLocal` 改成了「只允许一处、且必须有守卫」**（原来是无条件禁止）。
#: 原因：`require_principal` 现在必须查一次账号（停用/改密/改角色立即生效，见
#: `docs/changes/001-account-login-and-admin-console.md` §3.4），而 HTTP 侧那次查库
#: 走的是 `Depends(get_db)` 注入（不出现本名单里的名字）。
#: 唯一需要在依赖体系之外开会话的地方是 **WebSocket 握手**（`deps.ws_actor`）与
#: **审批路由在依赖之外解析身份**（`deps.principal_from_request`）——
#: 两者都必须 try/finally 关闭会话，否则连接池会被吃干（本仓实测过这个事故）。
#: 下面 `_bypass_exemptions` 用「同一行有豁免标记」来放行这两处，并把
#: 「为什么它安全」钉在代码里；**其余任何地方出现都仍然红**。
FORBIDDEN_CALLS = (
    "execute_readonly_query",
    "validate_sql",
    "mask_pii",
    "get_read_engine",
    "row_filter_for_customer",
    "assert_row_filter_present",
)

#: 允许出现上述名字的**行级**豁免：必须带理由，且必须紧邻代码。
#: 用「行」而不是「文件」豁免，是为了让「这个文件被整体放行」这件事不可能发生。
BYPASS_EXEMPTION_MARKER = "# 契约豁免: SessionLocal"

#: 禁止在**字符串字面量**里出现的 SQL 关键字（注释不算 —— 本仓的注释会解释为什么这里不查库）
SQL_KEYWORD_RE = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.IGNORECASE)


def _api_files() -> list[Path]:
    return sorted(path for path in API_DIR.rglob("*.py") if "__pycache__" not in path.parts)


def test_api_package_has_the_expected_modules() -> None:
    """文件清单本身就是契约（缺一个就会在 import 时暴露，但红在这里更好定位）。"""
    names = {path.name for path in _api_files()}
    expected = {
        "__init__.py",
        "chat.py",
        "config.py",
        "deps.py",
        "errors.py",
        "health.py",
        "main.py",
        "metrics.py",
        "middleware.py",
        "ratelimit.py",
        "schemas.py",
        "scope.py",
    }
    assert expected <= names, f"缺少模块：{sorted(expected - names)}"


@pytest.mark.parametrize("path", _api_files(), ids=lambda p: p.name)
def test_no_data_layer_imports(path: Path) -> None:
    """API 层不许直接 import 数据层/模型 SDK（取数走 `app.tools`，模型走 `app.llm`）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.split(".")[0] in FORBIDDEN_IMPORTS)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in FORBIDDEN_IMPORTS:
                found.append(node.module)
    assert not found, f"{path.name} 直接 import 了不该碰的库：{found}"


@pytest.mark.parametrize("path", _api_files(), ids=lambda p: p.name)
def test_no_security_bypass_calls(path: Path) -> None:
    """API 层不许自己执行 SQL / 校验 SQL / 脱敏 / 拼行级过滤（那些都是别处的唯一真相）。

    变更 001 起多了一条**行级**豁免机制（见 `FORBIDDEN_CALLS` 上方的说明）：
    只有带了 `BYPASS_EXEMPTION_MARKER` 的那一行才被放行，其余一律红。
    这样「谁被放行了」在代码里一眼可见、可 grep，而不会变成「某个文件整体不受管」。
    """
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source)
    hits: list[str] = []

    def _exempt(lineno: int) -> bool:
        if not (1 <= lineno <= len(source_lines)):
            return False
        return BYPASS_EXEMPTION_MARKER in source_lines[lineno - 1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in FORBIDDEN_CALLS and not _exempt(node.lineno):
                hits.append(name)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS:
            if not _exempt(node.lineno):
                hits.append(node.attr)
    assert not hits, f"{path.name} 调用了越界的函数：{sorted(set(hits))}"


def test_bypass_exemptions_are_minimal_and_documented() -> None:
    """豁免必须**少、且带理由**：这条用例防止豁免机制被滥用成「到处加标记」。

    判据有两条：
    1. 带豁免标记的行**不能超过 2 处**（当前只有 `deps._session()` 那一个函数）；
    2. 每个带标记的文件里，标记行**必须同时出现在注释里说明原因**（同一行即可）。
    数量一旦要涨，说明「会话生命周期只有一处」这条纪律正在被侵蚀 ——
    那时该做的是重构，而不是把上限调大。
    """
    marked: list[tuple[str, int]] = []
    for path in _api_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if BYPASS_EXEMPTION_MARKER in line:
                marked.append((path.name, number))
    assert len(marked) <= 2, f"契约豁免太多了（{len(marked)} 处）：{marked}；请先考虑重构"
    for name, number in marked:
        assert name == "deps.py", f"豁免只允许出现在 deps.py（会话边界），却在 {name}:{number}"


@pytest.mark.parametrize("path", _api_files(), ids=lambda p: p.name)
def test_no_sql_in_string_literals(path: Path) -> None:
    """API 层的字符串里不该出现 SQL 语句（文案里出现 `SELECT` 也算 —— 那说明有人在这写查询）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if SQL_KEYWORD_RE.search(node.value):
                hits.append(node.value[:80])
    assert not hits, f"{path.name} 的字符串里出现 SQL：{hits}"


def test_scope_table_covers_every_limited_path() -> None:
    """**限流映射必须闭合**：每个受保护的路径都要能查到自己的规则（否则会静默退回默认额度）。

    这条防的正是 `scope.py` 模块 docstring 里写的那种事故：改了 scope 名、忘了同步 `.env` 键
    或 `DEFAULT_RULES`，症状是「限流额度悄悄变成了默认值」而没有任何报错。
    """
    from app.api.ratelimit import DEFAULT_RULES
    from app.api.scope import SCOPE_BY_ROUTE, scope_name

    for (method, path), scope in SCOPE_BY_ROUTE.items():
        assert scope_name(method, path) == scope
        assert scope in DEFAULT_RULES, f"{scope} 没有默认额度（.env 没配时它会退回默认值）"


def test_limited_scopes_are_a_subset_of_the_scope_table() -> None:
    """中间件只对显式登记的路径限流，而登记的路径必须在 scope 表里有名字。"""
    from app.api.middleware import LIMITED_SCOPES
    from app.api.scope import SCOPE_BY_ROUTE

    assert LIMITED_SCOPES <= set(SCOPE_BY_ROUTE), f"这些路径限流了但没有 scope：{LIMITED_SCOPES - set(SCOPE_BY_ROUTE)}"


def test_every_scoped_route_is_actually_enforced() -> None:
    """★**反向**断言：每条登记了 scope 的业务路由都必须真的被中间件限流。

    ## 为什么必须有这一条（issue 003 的 P0-1 就是从这里漏过去的）

    上面那条只断言 `LIMITED_SCOPES ⊆ SCOPE_BY_ROUTE` —— 它能拦住「限流了但没 scope」，
    **拦不住反方向**：`scope.SCOPE_BY_ROUTE` 里有、`ratelimit.DEFAULT_RULES` 里也配了额度、
    唯独 `middleware.LIMITED_SCOPES` 里没有 → 中间件根本不去问限流器 → **额度完全不生效**，
    而且没有任何报错。实测症状：连续 40 次错误登录全是 401、一次 429 都没有，
    而 `POST /api/chat`（登记过）第 31 次起正常 429。

    限流要生效需要三处登记，缺任何一处都是「静默失效」。这条用例把三处钉在一起，
    以后新增受保护接口时**漏登记会直接红**，而不是等到安全评审才被发现。
    """
    from app.api.middleware import LIMITED_SCOPES
    from app.api.scope import SCOPE_BY_ROUTE

    missing = set(SCOPE_BY_ROUTE) - set(LIMITED_SCOPES)
    assert not missing, (
        "这些路径登记了 scope 却没有被中间件限流（额度配了也不生效）："
        f"{sorted(missing)}。请把它们加进 app/api/middleware.py::LIMITED_SCOPES，"
        "或从 scope.SCOPE_BY_ROUTE 里删掉。"
    )


def test_auth_endpoints_are_rate_limited() -> None:
    """★登录与刷新**必须**受限流保护（issue 003 的 P0-1 回归判据）。

    单列一条而不是只靠上面那条通用断言，是因为这两条路径的失效后果最严重：
    登录是唯一能用「猜」换到令牌的入口，不限流等于给暴力破解无限次尝试
    （`.env.example` 与 `scope.py` 的注释都是这么写的，但代码曾经没做到）。
    """
    from app.api.middleware import LIMITED_SCOPES
    from app.api.ratelimit import DEFAULT_RULES
    from app.api.scope import SCOPE_BY_ROUTE

    for path in ("/api/auth/login", "/api/auth/refresh"):
        route = ("POST", path)
        assert route in LIMITED_SCOPES, f"{path} 没有登记限流（额度不生效）"
        scope = SCOPE_BY_ROUTE[route]
        assert scope in DEFAULT_RULES, f"{path} 的 scope {scope} 没有默认额度"


def test_health_and_metrics_never_get_rate_limited() -> None:
    """探针与抓取不吃配额（`9.api.md` §2 的两个基础设施接口）。

    为什么值得一条用例：把 `/health` 加进限流表是一个**看起来很合理**的改动
    （「所有接口都该限流」），但依赖抖动时探针会被打成 429 → 编排系统判定服务不可用 →
    重启一个本来健康的进程。这条断言让这个改动必须先经过一次有意识的取舍。
    """
    from app.api.middleware import LIMITED_SCOPES
    from app.api.scope import UNLIMITED_PATHS

    limited_paths = {path for _, path in LIMITED_SCOPES}
    assert not (UNLIMITED_PATHS & limited_paths), "基础设施路径被加进了限流表"


def test_error_envelope_shape_is_stable() -> None:
    """错误信封只有一个形状（前端只写一套解析）。"""
    from app.api.errors import error_response

    response = error_response(status_code=418, code="teapot", message="我是茶壶", request_id="req-1")
    import json

    payload = json.loads(response.body)
    assert set(payload) == {"error", "request_id"}
    assert set(payload["error"]) == {"code", "message", "detail"}
    assert response.status_code == 418
    assert response.headers["X-Request-Id"] == "req-1"


def test_ui_business_paths_are_the_documented_ones(app) -> None:
    """对外路径与 `9.api.md` §2 一致（改路径要先改文档，反之亦然）。"""
    from app.api.config import DEFAULT_PREFIX, api_prefix

    schema = app.openapi()
    assert f"{DEFAULT_PREFIX}/chat" in schema["paths"]
    assert f"{DEFAULT_PREFIX}/chat/approve" in schema["paths"]
    assert "/health" in schema["paths"]
    assert "/metrics" in schema["paths"]
    # WebSocket 不进 OpenAPI（协议如此），所以从路由表断言它存在，并**拼上前缀**核对
    prefix = api_prefix()
    ws_paths = {f"{prefix}{path}" for path in _websocket_paths(app)}
    assert f"{DEFAULT_PREFIX}/chat/stream" in ws_paths, f"WebSocket 路由缺失：{ws_paths}"


def _websocket_paths(app) -> list[str]:
    """收集 app 里所有 WebSocket 路由的**内层路径**（不含 include 前缀）。

    ⚠️ FastAPI 0.143 把 `include_router` 的结果包成 `_IncludedRouter`，而它的
    `routes` 属性**不是内层的路由表**（实测：`getattr(route, "routes")` 返回一个 str 描述）。
    所以「翻 `app.routes` 找 WebSocket」会什么都找不到。

    这里用 FastAPI 0.143 的 `effective_route_contexts()`（公开结构，带 `original_route`
    与**生效路径**）。⚠️ 实测的一个转折：对它内部 `original_route.path` 读出来的
    **仍然是没带前缀的 `/chat/stream`**，而 `str(context)` 里才印着生效路径 ——
    依赖那种字符串解析太脆，所以本函数只返回内层路径，前缀由调用方拼（`api_prefix()`）。
    这样断言的就是「路由挂上了 + 前缀由配置决定」两件独立的事，而不是去猜框架的私有字段。
    """
    found: list[str] = []
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for context in contexts() if callable(contexts) else contexts:
            original = getattr(context, "original_route", None)
            if type(original).__name__ == "APIWebSocketRoute":
                found.append(str(getattr(original, "path", "")))
    return found
