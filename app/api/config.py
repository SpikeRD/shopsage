"""API 的运行参数：**前缀、限流额度、调试载荷开关、探活超时**。

口径与 `1.config.md` 一致：新配置项一律从 `app.config.settings.get_settings()` 读，
但**字段声明留在本模块的解析函数里**（`Settings` 是 `extra="allow"`，
所以 `.env` 里的 `API_*` 键能读到，不必为了加一个参数去改那个全局字段表）。

为什么不把这些参数写成模块级常量：

* **限流额度是运维参数**（要能按容量调整、能临时放宽），写死在代码里等于每次调整都发版；
* **前缀是部署参数**（网关/Ingress 常常统一加一层前缀），同理；
* **调试载荷开关是环境策略**（dev 开、prod 关），它决定「内部结构要不要出网」，
  属于必须有显式取值来源的那一类，不能靠「忘了关」的默认行为。

## `.env` 里新增的键

| 键 | 默认 | 说明 |
|---|---|---|
| `API_PREFIX` | `/api` | 路由前缀（`/health`、`/metrics`、`/docs` **不受它影响**，见下节） |
| `RATE_LIMIT_CHAT` | `30/60` | 非流式问答：60 秒内 30 次（`次数/窗口秒`） |
| `RATE_LIMIT_CHAT_STREAM` | `60/60` | 流式问答 |
| `RATE_LIMIT_CHAT_APPROVE` | `20/60` | 人工审批通道 |
| `API_DEBUG_PAYLOAD` | `false` | 是否在响应里带上 `visited` / `sql` 这类内部结构 |
| `API_PROBE_TIMEOUT` | `0.5` | `/health` 里 TCP 探活的超时（秒） |

未配置 = 用 `app/api/ratelimit.py` 里的默认额度（**不是不限流**）。

## 为什么健康检查与指标**不吃前缀**

`/health` 与 `/metrics` 是**基础设施**接口（K8s probe、Prometheus scrape），
它们的路径通常写在别人的配置里（`infra/` 的探针定义、Prometheus 的 `scrape_configs`）。
让它们跟着业务前缀走，会出现「改了 `API_PREFIX`，探针集体报错」这种最不该发生的事故。
所以业务路由挂在前缀下、这两个固定挂在根上。
"""

from __future__ import annotations

import logging
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 路由前缀的默认值（与 `9.api.md` §2 的 `/api/chat` 一致）
DEFAULT_PREFIX: Final[str] = "/api"

#: 前缀的卫生规则：必须是 `/xxx` 形状，末尾斜杠由规范化去掉（`/api/` + `/chat` 会变成 `/api//chat`）
_MALFORMED_PREFIX_HINT: Final[str] = "前缀必须形如 `/api`（以 `/` 开头、不含空白；末尾斜杠会被自动去掉）"


def api_prefix(settings: Any = None) -> str:
    """读取并规范化业务路由前缀。

    ⚠️ 两个真实的坑都在这里堵住（各对应一条用例）：

    1. **末尾斜杠**：`API_PREFIX=/api/` 会让 `APIRouter(prefix="/api/")` + `@router.post("/chat")`
       生成 `/api//chat` —— FastAPI 不会报错，只是**没有客户端能匹配上它**。
    2. **空串 / 非斜杠开头**：`API_PREFIX=api` 会生成 `api/chat`，
       而 ASGI 的 `path` 一定以 `/` 开头 → 永远匹配不上，症状同样是「404 但路由看起来在」。
       空串是合法配置（表示不加前缀），所以只有「非空且不以 `/` 开头」才判非法。
    """
    raw = _env_value(settings, "api_prefix", DEFAULT_PREFIX)
    value = str(raw or "").strip()
    if not value:
        return ""
    if not value.startswith("/") or any(ch.isspace() for ch in value):
        raise ValueError(f"API_PREFIX={raw!r} 非法：{_MALFORMED_PREFIX_HINT}")
    return value.rstrip("/")


def debug_payload_enabled(settings: Any = None) -> bool:
    """是否在响应里带内部结构（`visited` / `sql` / `guardrail_rules`）。"""
    raw = _env_value(settings, "api_debug_payload", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def rate_limit_specs(settings: Any = None) -> dict[str, str]:
    """从配置里取「接口 → 额度表达式」的原始字符串。

    表达式形如 `30/60`（60 秒 30 次）。**不配就用 `ratelimit.DEFAULT_RULES` 的默认值**
    （见 `rate_limit_rules`），所以这里只回传「配了的那些」。
    """
    out: dict[str, str] = {}
    for scope, key in (
        ("chat", "rate_limit_chat"),
        ("chat.stream", "rate_limit_chat_stream"),
        ("chat.approve", "rate_limit_chat_approve"),
        # 第 12 步：长任务提交（默认 10/60，理由见 `ratelimit.DEFAULT_RULES`）
        ("tasks", "rate_limit_tasks"),
    ):
        raw = str(_env_value(settings, key, "") or "").strip()
        if raw:
            out[scope] = raw
    return out


def parse_rate_limit(expression: str) -> tuple[int, float]:
    """`"30/60"` → `(30, 60.0)`（次数, 窗口秒）。

    支持三种写法，都是**人能一眼读懂**的：

    | 写法 | 含义 |
    |---|---|
    | `30/60` | 60 秒内 30 次（推荐，写全） |
    | `30` | 30 次 / 60 秒（省掉窗口，用默认窗口） |

    ⚠️ 非法值**启动即抛**（而不是退回默认）：把 `RATE_LIMIT_CHAT=abc` 静默当成默认额度，
    会让「我明明调小了额度，怎么没生效」变成一次线上排障 —— 这类配置错误必须 fail-fast，
    与 `Settings._validate_prod_requirements` 是同一条纪律。
    """
    text = str(expression).strip()
    if not text:
        raise ValueError("限流表达式为空：写成 `次数/窗口秒`，例如 `30/60`")
    limit_text, _, window_text = text.partition("/")
    try:
        limit = int(limit_text.strip())
    except ValueError as exc:
        raise ValueError(f"限流表达式 {expression!r} 的次数不是整数（例：`30/60`）") from exc
    window = 60.0
    if window_text.strip():
        try:
            window = float(window_text.strip())
        except ValueError as exc:
            raise ValueError(f"限流表达式 {expression!r} 的窗口不是数字（例：`30/60`）") from exc
    if limit <= 0 or window <= 0:
        raise ValueError(f"限流表达式 {expression!r} 必须都是正数（0 或负数会让接口永远 429）")
    return limit, window


def rate_limit_rules(settings: Any = None) -> dict[str, Any]:
    """把默认规则与 `.env` 覆盖合并成最终规则表（`scope -> RateLimitRule`）。"""
    from app.api.ratelimit import DEFAULT_RULES, RateLimitRule

    rules: dict[str, RateLimitRule] = dict(DEFAULT_RULES)
    for scope, expression in rate_limit_specs(settings).items():
        limit, window = parse_rate_limit(expression)
        rules[scope] = RateLimitRule(limit=limit, window_seconds=window)
        logger.info("限流额度被 .env 覆盖：scope=%s → %d 次 / %gs", scope, limit, window)
    return rules


def probe_timeout(settings: Any = None) -> float:
    """`/health` 里 TCP 探活的超时（秒）。

    0.5 秒是刻意的：探活是**同步调用者的等待**，而它检查的是「同网段的依赖」。
    比这更慢的话，依赖不通时 `/health` 自己就成了慢接口 —— 那不叫探活，叫放大故障。
    """
    raw = str(_env_value(settings, "api_probe_timeout", "") or "").strip()
    if not raw:
        return 0.5
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"API_PROBE_TIMEOUT={raw!r} 不是数字（单位：秒，例：`0.5`）") from exc
    if value <= 0:
        raise ValueError(f"API_PROBE_TIMEOUT={raw!r} 必须为正数")
    return value


def checkpoint_backend(settings: Any = None) -> str:
    """checkpoint 后端（`memory` / `postgres` / `redis`）。

    ⚠️ **默认 `memory`**（不默认 postgres）：默认值会被复制到所有调用方（含测试与脚本），
    一个「默默连生产库」的默认值是事故的开始 —— 这条纪律是 `8.graph.md` §4 第 1 条定的，
    这里只是把它接到配置上。要持久化就显式写 `CHECKPOINT_BACKEND=postgres`，
    而这个字符串出现在 `.env` 里，review 时看得见。
    """
    from app.graph.checkpointer import BACKENDS

    raw = str(_env_value(settings, "checkpoint_backend", "memory") or "memory").strip().lower()
    if raw not in BACKENDS:
        raise ValueError(f"CHECKPOINT_BACKEND={raw!r} 非法：只能是 {', '.join(BACKENDS)} 之一")
    return raw


def _env_value(settings: Any, field: str, default: Any) -> Any:
    """读一个 `API_*` 配置项。

    查找顺序与项目口径一致（`1.config.md`：**进程环境变量 > .env > 代码默认值**）：

    1. **`os.environ[<FIELD 大写>]`** —— 进程环境变量优先级最高，必须先看它；
    2. **`getattr(settings, field)`** —— 显式声明的字段（将来若把 `api_prefix` 提升成
       `Settings` 的正式字段，这里自动生效）；
    3. 代码默认值。

    ⚠️ **不要只依赖第 2 步**（本文件第一版就是这么写的，实测踩到）：`Settings` 虽然配了
    `extra="allow"`，但 pydantic-settings 并不把 `.env`/环境变量里的**任意**额外键放进去
    —— 实测 `API_DEBUG_PAYLOAD=1` 时 `getattr(get_settings(), "api_debug_payload")` 仍然
    **取不到**。但 pydantic-settings 会把 `.env` 的内容写进 `os.environ`，
    所以第 1 步能读到它。只走第 2 步的症状极具误导性：配置明明设置了，行为却是默认值、
    而且**没有任何报错**（§10.1 坑 4）。
    """
    import os

    from_env = os.environ.get(field.upper())
    if from_env is not None and str(from_env).strip() != "":
        return from_env
    if settings is None:
        try:
            from app.config import get_settings

            settings = get_settings()
        except Exception as exc:  # noqa: BLE001 - 配置不可读时退回默认值，不阻断起服务
            logger.debug("配置不可读，%s 用默认值：%s", field, exc)
            return default
    return getattr(settings, field, default)


__all__ = [
    "DEFAULT_PREFIX",
    "api_prefix",
    "checkpoint_backend",
    "debug_payload_enabled",
    "parse_rate_limit",
    "probe_timeout",
    "rate_limit_rules",
    "rate_limit_specs",
]
