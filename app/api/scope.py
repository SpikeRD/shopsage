"""接口名（scope）的**唯一映射表**：`方法 + 路径` → 限流的 scope 名。

为什么单独一个模块、而不是直接在中间件里 `path.replace("/", ".")`：

限流的额度是按 scope 配的（`RATE_LIMIT_CHAT=30/60`），而 scope 名同时出现在三个地方：

1. `.env` 的键（人写的）；
2. `ratelimit.DEFAULT_RULES` 的字典键（默认额度）；
3. 中间件与路由里 `check(caller, scope)` 的实参。

三处各写一遍字符串，改一个忘一个的症状是**限流静默失效**（查的 scope 没有规则 →
退回 `DEFAULT_RULE` → 额度变成 60/分钟，而你以为配的是 30）。所以映射收在这里，
并且用例直接断言「每个受保护的路径都能查到它自己的规则」。

⚠️ 路径参数（如 `/api/chat/{id}`）不在这里出现：本项目目前的业务路径都是固定路径。
将来若加了带路径参数的接口，映射函数要按 pattern 匹配 —— 那时**不要**退回
「用原始路径当 scope」：那会让每个 id 各占一个额度桶（限流形同不限）。
"""

from __future__ import annotations

from typing import Final, Protocol

#: 受保护的路径 → scope 名（**方法也参与**：`POST /x` 与 `GET /x` 是两种代价）
SCOPE_BY_ROUTE: Final[dict[tuple[str, str], str]] = {
    ("POST", "/api/chat"): "chat",
    # ⚠️ 变更 003 **删除了** `("POST", "/api/chat/approve")`：对客人工审批闸门整体移除后，
    # 那条路径在任何角色下都恒 409（图里再也不会注册闸门节点，见 `graph/config.GATED_ROLES`）。
    # 留着一个永远不会成功、却占着额度桶与文档位置的接口，只会让下一个人以为它还能用。
    # 第 12 步：提交长任务。**它必须限流**，而且要比 chat 更严：一次提交就是一个
    # 后台长任务（可能几分钟、可能几次模型调用），而被滥用的代价落在**队列**上
    # （积压 → 所有人的任务都变慢），不像 chat 那样只影响调用者自己。
    ("POST", "/api/tasks"): "tasks",
    # ── 变更 001：账号接口 ────────────────────────────────────────────────────
    # ⚠️ **登录与刷新必须显式限流**，而且要比 chat 严得多：
    #   · 登录是**唯一**能用「猜」换到令牌的入口 —— 不限流等于给暴力破解开了无限次尝试；
    #   · 它用不上现有的桶键（未登录时没有令牌，桶退回来源 IP），所以「按账号限流」
    #     在这里做不到 —— 那正是**需要**更严的额度与更长的窗口的原因（见 `.env.example`）。
    # 刷新也要限：刷新令牌有效期 14 天，不限流的话它就是一个「可无限尝试的凭证校验口」。
    ("POST", "/api/auth/login"): "auth.login",
    ("POST", "/api/auth/refresh"): "auth.refresh",
    # ── 变更 003：退款申请与审批（**两条写路径都必须限流**）───────────────────
    # 申请：一次调用在库里多一行、并且**占用该订单的可退额度**（额度是有限资源）。
    # 不限流时，一个脚本可以把「同一客户的所有订单」在几秒内全部申请一遍 ——
    # 员工审批台会被瞬时灌满，而真正的紧急单埋在噪音里。
    #
    # ⚠️ 路径**不带路径参数**：`SCOPE_BY_ROUTE` 的键是精确的 `(method, path)`，
    # 而中间件的判定也是 `(method, path) in LIMITED_SCOPES`。带参数的路径
    # （如 `/api/refunds/{id}/decision`）**匹配不上**，于是它会静默退回「不限流」——
    # 那正是 `tests/api/test_api_contracts.py::test_every_scoped_route_is_actually_enforced`
    # 想防的事。所以审批口写成 `/api/refunds/decision`（退款单号在请求体里）。
    ("POST", "/api/refunds"): "refunds.request",
    ("POST", "/api/refunds/decision"): "refunds.decide",
}

#: WebSocket 的 scope 名（握手成功后由路由自己判定；路径不在 HTTP 的映射里）
WS_SCOPE_CHAT_STREAM: Final[str] = "chat.stream"

#: 不受限流影响的路径（显式登记，且**不**进 `LIMITED_SCOPES`）。
#: 写在这里是为了让「为什么它们不限流」有一个可被 review 的落点。
UNLIMITED_PATHS: Final[frozenset[str]] = frozenset({"/health", "/metrics", "/docs", "/openapi.json", "/redoc"})


class HasRateRule(Protocol):
    def rule_for(self, scope: str) -> object:
        ...


def scope_name(method: str, path: str) -> str:
    """`(POST, /api/chat)` → `chat`。

    未登记的路径返回 `f"{method.lower()} {path}"`：**这个返回值一定会查不到规则**，
    于是退回默认额度并打一条 DEBUG 日志 —— 这正是我们要的行为（新增接口不会因为
    「忘了登记」而完全裸奔）。同时它不会被 `LIMITED_SCOPES` 命中，
    所以也不会给每个未知路径造一个桶。
    """
    return SCOPE_BY_ROUTE.get((str(method).upper(), str(path)), f"{str(method).lower()} {path}")


__all__ = ["SCOPE_BY_ROUTE", "UNLIMITED_PATHS", "WS_SCOPE_CHAT_STREAM", "scope_name"]
