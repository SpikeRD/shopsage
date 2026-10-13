"""MCP 层的配置面：**外部客户端是谁**，以及由此决定的可见范围。

为什么 MCP 需要一个「身份」配置
--------------------------------

`15.mcp.md` 第 4 节第 2 条要求「MCP 与内部调用共用同一安全层（D1 不因入口不同而失效）」。
但第 6 步的工具安全层里有一条**只有入口知道**的量：

* `4.security.md` 的 D1 有两半 —— 一半是「SQL 长什么样」（`sql_guard`，工具自己会做），
  另一半是「这条连接能看谁的**行**」（`db/queries.py` 的行级过滤，靠 `requester_user_id` 驱动）。
* 内部 Agent 走这条路时，`user_id` 来自**已鉴权的会话**（第 9 步的 JWT → `Actor`）。
  MCP 没有 HTTP 会话：它可能是一个本地 stdio 进程（用户的 Cursor/Claude 桌面端），
  也可能是团队共享的一台 streamable-http 服务。

所以 MCP 侧必须显式回答「这次调用以谁的身份执行」，答案就是本模块的两个配置项。
**它必须在服务端配置里，不能是工具参数** —— 让客户端用参数申报自己的身份，
等于把 D1 的第二半交给调用方自觉（与第 9 步「请求体里没有 user_id」是同一条纪律）。

两种策略（`MCP_POLICY`）
------------------------

| 策略 | 身份 | 额外强制 |
|---|---|---|
| `customer`（默认） | 必须配 `MCP_ACTOR_USER_ID` | `sql_query` 的 SQL 必须带 `WHERE user_id = :user_id` 且绑定值必须等于该身份 |
| `internal` | 由部署方负责（本机受信进程 / 前置网关已鉴权） | 无额外强制，但 RBAC 与守卫照旧生效 |

**默认取 `customer`**：默认值要选「配错了也不会越权」的那一个。`internal` 是**放宽**，
必须由部署者显式写一行配置来承担后果（`docs/runbook/15.mcp-runbook.md` §4 有风险清单）。

为什么不做成 `app/config/settings.py` 里的新字段
------------------------------------------------

与第 13、14 步同一条口径（`13.cache-runbook.md` §1.4 / `14.eval-runbook.md` §1.4）：
`Settings` 里放的是**全进程都要知道**的东西。MCP 的身份只对 MCP 这一个入口有意义，
放进全局配置会让「我只想跑个 API」的场景也必须回答「MCP 以谁的身份跑」。

但读取顺序仍然完整（`constructor > os.environ > .env > default`）：
`os.environ` 排在 `.env` 前面是刻意的 —— 同一台机器上跑两种用途的进程时，
靠环境变量临时覆盖比改共享的 `.env` 安全（与 `app/api/config.py::_env_value` 同一口径）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any, Final

from app.security.policy import CUSTOMER_ROLE, ROW_FILTER_COLUMN

# ── MCP 服务身份（出现在 initialize 握手里，客户端据此展示「连上了谁」）──────────────
SERVER_NAME: Final[str] = "shopsage-mcp"
SERVER_TITLE: Final[str] = "ShopSage 工具服务"
SERVER_VERSION: Final[str] = "1.0.0"

#: 策略常量。写成常量而不是散落的字符串字面量：拼错 `custmer` 会静默退化成「更宽的那一档」，
#: 所以 `McpSettings.validate()` 用白名单拦它（默认拒绝，见 `4.security.md` 第 4 节第 3 条）。
POLICY_CUSTOMER: Final[str] = "customer"
POLICY_INTERNAL: Final[str] = "internal"
POLICIES: Final[tuple[str, ...]] = (POLICY_CUSTOMER, POLICY_INTERNAL)

#: 审计 `caller` 的前缀（`6.tools-runbook.md` §3.3 的约定取值之一：`mcp:sql_query`）。
#: 有了它，审计日志里「这次调用来自 MCP 而不是内部 Agent」是一眼可见的。
CALLER_PREFIX: Final[str] = "mcp:"

#: 对外工具名的**唯一真相**（`15.mcp.md` 第 2 节的四个）。
#: ⚠️ 这四个名字与 `app/tools/*.py` 里的 `TOOL_NAME` 必须一致 ——
#: `tests/mcp/test_mcp_contracts.py` 会遍历真实对象对账，而不是抄一份清单。
EXPOSED_TOOLS: Final[tuple[str, ...]] = (
    "sql_query",
    "retrieve_knowledge",
    "create_ticket",
    "build_chart",
)

#: 工具的默认说明（供客户端发现）。写成常量而不是散在四个函数 docstring 里：
#: 描述是**对外契约**的一部分（客户端模型靠它决定怎么调），契约测试要能逐个断言，
#: 而不是靠人盯四段文本有没有被改坏。
TOOL_DESCRIPTIONS: Final[dict[str, str]] = {
    "sql_query": (
        "在只读 PostgreSQL 上执行一条单语句 SELECT（表/列白名单 + 强制 WHERE + 行数上限）。"
        f"必须用 :{ROW_FILTER_COLUMN} 命名参数绑定过滤值，不支持字面量 WHERE 条件。"
        "返回 {rows, meta}：meta.row_count / truncated / audit_id 可用于分诊。"
    ),
    "retrieve_knowledge": (
        "检索 ShopSage 知识库（向量 + 关键词混合检索 + 精排），只返回带 source 溯源的片段。"
        "无溯源的命中会被丢弃（不返回无法核对的内容）。"
    ),
    "create_ticket": (
        "为客户建一张工单（写路径的唯一出口，幂等）。"
        "idempotency_key 由调用方提供：同一个 (user_id, key) 重复调用只会建一张、返回同一个 ticket_id。"
    ),
    "build_chart": (
        "把查询结果组装成结构化图表 spec（shopsage.chart/v1），不返回图片。"
        "载荷只含 x 与系列列（其余列不进 spec）。前端/MCP 客户端按 spec 自行渲染。"
    ),
}

#: `initialize` 时给客户端的说明（把边界写清楚，比多塞几个工具更有用）
SERVER_INSTRUCTIONS: Final[str] = (
    "ShopSage 的四个工具能力，与内部 Agent 共用同一套安全层：\n"
    "1) 写操作只能走 create_ticket（预定义函数 + 幂等），没有通用写库能力；\n"
    "2) sql_query 过 SQL 静态守卫（写语句 / 多语句 / 无 WHERE / 危险函数 / 白名单外一律拒绝），"
    "并在只读数据库角色上执行；\n"
    "3) 检索只返回带来源的片段，无溯源的命中会被丢弃；\n"
    "4) 身份由服务端配置（MCP_POLICY / MCP_ACTOR_USER_ID）决定，不是工具参数。"
)


def _env_value(field: str, default: Any) -> Any:
    """读一个 `MCP_*` 配置项：**进程环境变量 > .env > 代码默认值**（`1.config.md` 的口径）。

    ⚠️ 为什么本文件不 `from app.api.config import _env_value`（本步实测撞到的坑）：
    `app/api/config.py` 会 import `app.api` 那一串（schemas / deps / main），
    于是 `import app.mcp` 之后 `fastapi` 与 `uvicorn` 全被拖进 `sys.modules` ——
    这直接违反了本层「薄壳、可独立部署」的定位（契约测试
    `test_mcp_package_does_not_import_fastapi` 会红）。
    十行读取逻辑复制一份是可以接受的代价；真正会漂的是**语义**（优先级顺序），
    所以这里用与那边完全相同的顺序，并在两处都写明。

    ⚠️ 为什么必须先看 `os.environ`：pydantic-settings 会把 `.env` 的内容写进
    `os.environ`，但**不会**把「本组件不认识的键」放进 `Settings` 对象
    （实测 `API_DEBUG_PAYLOAD=1` 时 `getattr(get_settings(), "api_debug_payload")` 取不到）。
    只看 `Settings` 的症状是「配置明明写了、行为却是默认值，且没有任何报错」。
    """
    from_env = os.environ.get(field.upper())
    if from_env is not None and str(from_env).strip() != "":
        return from_env
    try:  # 兼容「将来把 MCP 配置提升成 Settings 正式字段」的情形（extra="allow" 不保证）
        from app.config import get_settings

        declared = getattr(get_settings(), field, None)
    except Exception:  # noqa: BLE001 - 配置不可读时退回默认值，不阻断构建
        declared = None
    return default if declared is None else declared


@dataclass(frozen=True)
class McpSettings:
    """MCP 服务的配置面（每一项都有默认值，`validate()` 给出「配错了会怎样」）。"""

    #: 暴露范围：`customer` 时 `sql_query` 强制行级过滤、`create_ticket` 强制本人
    policy: str = POLICY_CUSTOMER
    #: 身份 user_id（`customer` 策略必填；`internal` 可空）
    actor_user_id: int | None = None
    #: 身份角色：进 RBAC 判定（对客 < 客服 < 员工…，见 `rbac.ROLE_PERMISSIONS`）
    actor_role: str = CUSTOMER_ROLE
    #: 单次检索的条数上限（只能比工具层的 20 更严）
    max_top_k: int = 10
    #: `sql_query` 的单次行数上限（只能比 `policy.MAX_ROWS`＝200 更严）
    max_rows: int = 100
    #: `build_chart` 的点上限（同上：对外入口比内部更严）
    max_chart_points: int = 50

    # ── 校验与派生 ──────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """返回问题清单（空 = 可用）。**不做副作用**，方便测试与启动期 fail-fast 共用。"""
        problems: list[str] = []
        if self.policy not in POLICIES:
            problems.append(
                f"policy 必须是 {' 或 '.join(POLICIES)} 之一，收到 {self.policy!r}"
                "（策略名拼错会退化成更宽的那一档，所以这里拒绝而不是取默认值）"
            )
        if self.actor_user_id is not None and (
            isinstance(self.actor_user_id, bool) or self.actor_user_id < 1
        ):
            problems.append(f"actor_user_id 必须是正整数或 None，收到 {self.actor_user_id!r}")
        if self.policy == POLICY_CUSTOMER and self.actor_user_id is None:
            problems.append(
                "policy=customer 时必须配置 MCP_ACTOR_USER_ID（行级过滤的唯一依据）："
                "没有它就无法回答「这次调用能看谁的行」，而那正是 D1 的第二半。"
                "若这个 MCP 服务确实是本机受信的内部用途，请显式改成 MCP_POLICY=internal"
                "（这是一次要认下来的放宽，见 runbook §4）。"
            )
        if not str(self.actor_role).strip():
            problems.append("actor_role 不能为空（RBAC 默认拒绝：未登记角色没有任何权限）")
        for name in ("max_top_k", "max_rows", "max_chart_points"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                problems.append(f"{name} 必须是正整数，收到 {value!r}")
        return problems

    @property
    def is_customer_scoped(self) -> bool:
        """对客策略：`sql_query` 必须带行级过滤，`create_ticket` 只能建在自己名下。"""
        return self.policy == POLICY_CUSTOMER

    def identity(self):
        """本服务声明的身份（`CustomerIdentity`）。配置不可用时抛 `ValueError`。"""
        from app.security.rbac import CustomerIdentity

        _require_valid(self)
        return CustomerIdentity(user_id=int(self.actor_user_id or 0), role=self.actor_role)

    def caller(self, tool: str) -> str:
        """审计用的 `caller`（`mcp:sql_query`）—— 入口在审计里必须可区分。"""
        return f"{CALLER_PREFIX}{tool}"

    # ── 构造 ────────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, **overrides: Any) -> "McpSettings":
        """按 `constructor > os.environ > .env > default` 读取，并做启动期校验。

        字段名 → 环境变量名的映射是**机械的**（`actor_user_id` → `MCP_ACTOR_USER_ID`），
        所以这里不手写六遍：把名字拼出来查，查不到就走默认值。
        校验的落点在这里而不是每个使用点：一个「身份不明」的 MCP 服务跑起来之后，
        每一条越权查询都会看起来像正常调用（审计里只有 `mcp:sql_query`，没有「谁」）。
        """
        defaults = cls()
        values: dict[str, Any] = {}
        for field in fields(cls):
            fallback = getattr(defaults, field.name)
            if field.name in overrides:
                values[field.name] = overrides[field.name]
                continue
            raw = _env_value(f"mcp_{field.name}", None)
            if raw is None:
                values[field.name] = fallback
            elif fallback is None or isinstance(fallback, int) and not isinstance(fallback, bool):
                values[field.name] = _to_int(raw, field=field.name)
            else:
                values[field.name] = raw

        settings = cls(**values)
        _require_valid(settings)
        return settings


def _require_valid(settings: McpSettings) -> None:
    problems = settings.validate()
    if problems:
        raise ValueError("MCP 配置不可用：" + "；".join(problems))


def _to_int(raw: Any, *, field: str) -> int | None:
    """环境变量永远是字符串：空串 = 没配（`None`），非法值直接报错而不是静默取 0。

    实测踩过的形态：`MCP_ACTOR_USER_ID=` 在 `.env` 里留空是很自然的写法，
    若把它当成 0 或抛未捕获的 `ValueError`，症状分别是「以 user 0 的身份查」与
    「服务起不来但没有可读报错」—— 两种都比一条显式报错难查。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MCP 配置项 {field} 必须是整数或留空，收到 {raw!r}") from exc


@lru_cache(maxsize=4)
def _cached(policy: str, actor_user_id: int | None, actor_role: str) -> McpSettings:
    """按「影响身份的三个字段」缓存一个实例（同参数只解析一次 `.env`）。"""
    settings = McpSettings(policy=policy, actor_user_id=actor_user_id, actor_role=actor_role)
    _require_valid(settings)
    return settings


def get_settings(**overrides: Any) -> McpSettings:
    """进程内单例（同身份参数只解析一次）。测试要造别的形态时直接 `McpSettings(...)`。"""
    if overrides:
        return McpSettings.from_env(**overrides)
    env = McpSettings.from_env()
    return _cached(env.policy, env.actor_user_id, env.actor_role)


__all__ = [
    "CALLER_PREFIX",
    "EXPOSED_TOOLS",
    "POLICIES",
    "POLICY_CUSTOMER",
    "POLICY_INTERNAL",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "SERVER_TITLE",
    "SERVER_VERSION",
    "TOOL_DESCRIPTIONS",
    "McpSettings",
    "get_settings",
]
