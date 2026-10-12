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
    ("POST", "/api/chat/approve"): "chat.approve",
    # 第 12 步：提交长任务。**它必须限流**，而且要比 chat 更严：一次提交就是一个
    # 后台长任务（可能几分钟、可能几次模型调用），而被滥用的代价落在**队列**上
    # （积压 → 所有人的任务都变慢），不像 chat 那样只影响调用者自己。
    ("POST", "/api/tasks"): "tasks",
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
