"""对外接口层（第 9 步）：FastAPI 路由 / WebSocket 流式 / 限流中间件 / JWT 鉴权。

`9.api.md` §4 第 4 条把这一层的职责收成一句话：**「鉴权 → 校验 → 转交图 → 格式化返回」**。
本包的边界据此划死：

| 子模块 | 职责 | 不做什么 |
|---|---|---|
| `deps` | 鉴权（JWT）、身份与会话派生、限流/图的取用 | 不解析请求体（body 只能被消费一次）、不判断业务权限 |
| `schemas` | 请求/响应/帧/错误的数据契约 | 不含逻辑 |
| `errors` | 域异常 → HTTP 的映射表（**唯一一处**） | 不重写域异常（那会丢掉 `rule` / `missing` 字段） |
| `middleware` | 请求 id + 访问日志 + 限流 | 不做鉴权（那是依赖的事） |
| `ratelimit` | 滑动窗口计数（Redis / 内存两种后端） | 不做业务配额（如「按角色给不同额度」——那要靠 `.env` 配） |
| `metrics` | 进程内计数器/直方图 → Prometheus 文本格式 | 不做节点级埋点（第 11 步） |
| `health` | `/health`（恒 200）与 `/metrics` | 不做深度检查（那是 `scripts/db_check.py`） |
| `chat` | 三条业务路由：问答 / 流式 / 审批 | **不含业务逻辑**：不出 SQL、不写 prompt、不判权限 |

## 对外入口

```python
from app.api import create_app                      # ASGI 应用（uvicorn app.main:app）
from app.api import SCOPE_BY_ROUTE, LIMITED_SCOPES  # 哪些路径吃配额（显式登记）
```

⚠️ 三条纪律（与 `9.api.md` §4 逐条对应）：

1. **身份只来自令牌**：请求体里没有 `user_id` / `role`，`thread_id` 由服务端派生（`deps`）。
2. **不做业务**：本包里出现 SQL 拼装、prompt 文本、脱敏规则就是越界（用例用源码检查钉住）。
3. **错误只有一个形状**：所有 4xx/5xx 都由 `errors.error_response` 产出（前端只写一套解析）。
"""

from app.api.config import (
    api_prefix,
    debug_payload_enabled,
    parse_rate_limit,
    probe_timeout,
    rate_limit_rules,
)
from app.api.errors import ApiError, error_response, install_exception_handlers
from app.api.main import create_app, make_graph_factory
from app.api.metrics import MetricsRegistry, status_class
from app.api.middleware import LIMITED_SCOPES, ApiMiddleware, new_request_id
from app.api.ratelimit import (
    DEFAULT_RULES,
    MemoryRateLimitStore,
    RateLimitDecision,
    RateLimitRule,
    RateLimiter,
    RedisRateLimitStore,
    open_rate_limiter,
)
from app.api.scope import SCOPE_BY_ROUTE, UNLIMITED_PATHS, scope_name

__all__ = [
    "DEFAULT_RULES",
    "LIMITED_SCOPES",
    "SCOPE_BY_ROUTE",
    "UNLIMITED_PATHS",
    "ApiError",
    "ApiMiddleware",
    "MemoryRateLimitStore",
    "MetricsRegistry",
    "RateLimitDecision",
    "RateLimitRule",
    "RateLimiter",
    "RedisRateLimitStore",
    "api_prefix",
    "create_app",
    "debug_payload_enabled",
    "error_response",
    "install_exception_handlers",
    "make_graph_factory",
    "new_request_id",
    "open_rate_limiter",
    "parse_rate_limit",
    "probe_timeout",
    "rate_limit_rules",
    "scope_name",
    "status_class",
]
