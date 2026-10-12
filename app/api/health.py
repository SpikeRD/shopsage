"""探活与指标两个基础设施接口：`GET /health`、`GET /metrics`（`9.api.md` §2）。

这两个接口**不挂在业务前缀下**（见 `app/api/config.py` 的说明）：它们的路径写在别人的配置里
（K8s probe、Prometheus `scrape_configs`），跟着 `API_PREFIX` 走会让「改前缀 → 探针集体报错」
成为一次事故。

## `/health` 的两个刻意的决定

### ① 恒返回 200（依赖故障写在 body 里）

`/health` 同时被「存活探针」和「就绪探针」用。存活探针拿到非 2xx 会**重启进程** ——
而依赖（Postgres/Redis）不通时进程本身完全健康，重启它只会把「依赖故障」放大成
「全站重启 + 连接风暴」。所以：

* 依赖故障 → `status="degraded"` / `"down"`，HTTP 仍是 200；
* 「进程真的死了」由根本没响应来表达（那是探针语义里最强的信号，不需要状态码帮忙）。

要「依赖不通就别切流量」的话，读 `components` 里的状态做决策（第 19 步的部署编排）。

### ② 探活用 TCP，不引入数据库/Redis 客户端

* **零依赖**：`app.db` 在 import 期就会建连接池（`app/db/session.py`），
  在探活路径上引入它 = 让「探活」变成「初始化一整套 ORM」；
* **零副作用**：TCP 连接不会在目标上留下会话、不会占连接池名额；
* **够用**：探活要回答的是「这个 host:port 通不通」，不是「SQL 能不能跑」。
  真正的深度检查（能不能查、迁移到没到 head）是 `scripts/db_check.py` 的活，
  它不该在每个探针周期里跑一遍。

⚠️ 「能连上」不等于「能用」（端口后面的进程可能是个连错库的实例）。
所以 `/health` 的定位是**快速分诊**，不是**验收**：它回答「依赖栈起来了吗」，
验收靠 `scripts/db_check.py` / `scripts/redis_checkpointer_probe.py`。

## `/metrics` 的格式

标准 Prometheus 文本格式 0.0.4（`Content-Type: text/plain; version=0.0.4; charset=utf-8`），
所以第 11 步接 Prometheus 时只需要加一行 `scrape_configs`，不用改这里。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response

from app.api.schemas import ComponentHealth, HealthResponse

router = APIRouter(tags=["infra"])

#: 服务版本（与 `pyproject.toml` 的 `version` 对齐；用于「这个故障是哪一版引入的」）
SERVICE_VERSION: Final[str] = "0.1.0"

#: TCP 探活的默认超时（可被 `API_PROBE_TIMEOUT` 覆盖，见 `config.probe_timeout`）
DEFAULT_PROBE_TIMEOUT: Final[float] = 0.5

#: Prometheus 文本格式的 Content-Type（`version=0.0.4` 是抓取端认的版本号）
PROMETHEUS_CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"


@dataclass(frozen=True)
class Endpoint:
    """一个待探活的目标。`required=False` 表示它挂了只降级、不判 `down`。"""

    name: str
    host: str
    port: int
    required: bool = True


def endpoint_from_url(url: str, *, name: str, default_port: int, required: bool = True) -> Endpoint | None:
    """`postgresql://u:p@host:5432/db` → `Endpoint(host, port)`；解析不出来返回 None。

    为什么要处理 None：连接串是**配置**，配置可能为空（本机没配 `REDIS_URL` 时
    `redis_url` 就是空串）。空配置不该让 `/health` 报「依赖 down」——
    那会把「没启用这个依赖」和「依赖坏了」混成同一种症状。
    """
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = parts.hostname or ""
    if not host:
        return None
    return Endpoint(name=name, host=host, port=int(parts.port or default_port), required=required)


def probe_tcp(endpoint: Endpoint, *, timeout: float) -> ComponentHealth:
    """一次 TCP 连接尝试（成功/失败都带耗时，失败带**一句能照做的话**）。

    ⚠️ 耗时是探活的核心信息之一：`down` 与「连上了但用了 400ms」是两种不同的隐患，
    只看 up/down 会把后者一直藏起来。
    """
    started = time.perf_counter()
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=timeout):
            pass
    except OSError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return ComponentHealth(
            name=endpoint.name,
            status="down",
            detail=(
                f"{endpoint.host}:{endpoint.port} 不可达（{type(exc).__name__}: {exc}）。"
                "本机没有 Docker，依赖栈在 VMware-Linux 上 —— 先确认 .env 里的 host 是 Linux 虚拟机 IP"
                "（见 docs/runbook/2.infra-runbook.md）"
            ),
            latency_ms=round(elapsed, 2),
        )
    elapsed = (time.perf_counter() - started) * 1000
    return ComponentHealth(
        name=endpoint.name,
        status="ok",
        detail=f"{endpoint.host}:{endpoint.port} 可连接",
        latency_ms=round(elapsed, 2),
    )


def rate_limit_component(request: Request) -> ComponentHealth:
    """限流后端的健康状况（**永不**报 down，理由见下）。

    内存限流是**有意的降级**（`app/api/ratelimit.py` 的取舍）：它让服务继续可用，
    代价是多副本下额度被放大。所以这里的状态是 `skipped` + 一句说明，
    而不是 `down` —— 把它算进「down」会让 `/health` 在 Redis 挂掉时显示全站不可用，
    而实际上业务是好的（这正是「探活要能反映真实可用性」的要求）。
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    backend = getattr(limiter, "store", None)
    name = getattr(backend, "name", "unknown")
    if name == "redis":
        return ComponentHealth(name="rate_limit", status="ok", detail="限流后端 = redis（多副本共享计数）")
    return ComponentHealth(
        name="rate_limit",
        status="skipped",
        detail="限流后端 = memory（Redis 不可达时的降级：多副本下额度会被放大到「副本数 × limit」）",
    )


def observability_component(request: Request) -> ComponentHealth:
    """可观测性三件套的状态（第 11 步）。

    ## 为什么它**永不**报 `down`

    与 `rate_limit` 同一条理由：埋点是**旁路**，它挂了业务照跑。
    把「OTel 没配端点」算成 down，会让 `/health` 在一个完全健康的服务上显示全站不可用 ——
    而那正是探针最不该做的事（第 9 步 `health.py` 的模块 docstring 里已经定过这个口径）。

    ## 三种状态分别在说什么

    | 状态 | 含义 | 该不该处理 |
    |---|---|---|
    | `ok` | 埋点开着，且至少记录了 1 条 span | 不用 |
    | `skipped` | 没配 `OTEL_ENDPOINT`（span 只留内存）或 `OBSERVABILITY_ENABLED=false` | **不用** —— 这是开发默认值 |
    | `degraded` | 埋点开着但**一条 span 都没有** | 要查：可能是「还没流量」，也可能是埋点没接上 |

    `degraded` 的措辞刻意把两种可能都写出来：只看 `degraded` 会让人立刻去查代码，
    而实际上「服务刚启动、还没有请求」是更常见的原因。
    """
    state = getattr(request.app.state, "telemetry", None)
    if state is None:
        return ComponentHealth(
            name="observability",
            status="skipped",
            detail="telemetry 未装配（未走 create_app 的 lifespan？）——业务不受影响",
        )
    if not state.enabled:
        return ComponentHealth(
            name="observability",
            status="skipped",
            detail="OBSERVABILITY_ENABLED=false：所有埋点退化成 no-op（压测/排障对照用）",
        )
    spans = len(state.exporter.records)
    if spans == 0:
        return ComponentHealth(
            name="observability",
            status="degraded",
            detail=(
                "埋点已启用但还没有记录到任何 span（可能是**还没有请求**，也可能是埋点没接上）。"
                f"外发端点：{state.otel_endpoint or '（未配，span 只留内存）'}"
            ),
        )
    return ComponentHealth(
        name="observability",
        status="ok",
        detail=(
            f"已记录 {spans} 条 span；外发端点：{state.otel_endpoint or '（未配，span 只留内存）'}；"
            f"/metrics 见 shopsage_agent_duration_seconds 等系列"
        ),
    )


def cache_component(request: Request) -> ComponentHealth:
    """缓存组件的状态（第 13 步）。

    ## 为什么缓存也需要一个 `/health` 组件

    它**永远不会让服务不可用**（缓存是优化：挂了业务照跑，只是慢一点、贵一点）。
    但它有一个**必须被看见**的降级形态：Redis 挂掉之后缓存会回落到「每个进程一份」的内存，
    于是多副本部署下命中率被副本数除 —— 而业务侧完全看不出来
    （接口照常返回、日志只有一条 WARNING）。

    ## 四种状态分别在说什么

    | 状态 | 含义 | 该不该处理 |
    |---|---|---|
    | `ok` | 条目存储与预期一致（Postgres 表在 / Redis 通） | 不用 |
    | `skipped` | 缓存没装配（没走 `create_app` 的 lifespan？） | 不用（业务不受影响） |
    | `degraded` | **存储降级了**（回落进程内存 / 缓存表不存在） | 要查：命中率会悄悄掉 |
    | `down` | 配置本身不合法（阈值/策略写错） | 要查：它会让缓存要么永不命中、要么答错问题 |

    ⚠️ 「配置不合法」为什么算 `down` 而不是 `degraded`：阈值写反（`floor > threshold`）
    会让缓存**永远不命中**（成本白花），而 `time_sensitive_policy` 写成别的值
    可能让强时效数据被缓存（**答错**）。这两件事都比「慢一点」严重，
    所以它必须比 `degraded` 更醒目。
    """
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        return ComponentHealth(
            name="cache",
            status="skipped",
            detail="语义缓存未装配（业务不受影响，只是每次问答都要走完整链路）",
        )
    try:
        state = cache.health()
    except Exception as exc:  # noqa: BLE001 - 探活失败也要给一句能读的话
        return ComponentHealth(
            name="cache",
            status="degraded",
            detail=f"缓存自检失败（{type(exc).__name__}）：{exc}",
        )
    problems = list(state.problems)
    if problems:
        return ComponentHealth(name="cache", status="down", detail="；".join(problems))
    stats = state.stats
    detail = (
        f"存储={state.store}"
        f"{'（**降级**：Redis 不可用，回落进程内存 —— 命中率会被副本数除）' if state.degraded else ''}；"
        f"条目={state.entry_store.get('entries', '?')}；"
        f"本轮命中/未命中/失效={stats.get('hit', 0)}/{stats.get('miss', 0)}/{stats.get('stale', 0)}"
    )
    return ComponentHealth(
        name="cache", status="degraded" if state.degraded else "ok", detail=detail
    )


@router.get("/health", response_model=HealthResponse, summary="健康检查（依赖状态在 body 里）")
def health(request: Request) -> HealthResponse:
    settings = getattr(request.app.state, "settings", None)
    timeout = float(getattr(request.app.state, "probe_timeout", None) or DEFAULT_PROBE_TIMEOUT)

    components: list[ComponentHealth] = [
        rate_limit_component(request),
        observability_component(request),
        cache_component(request),
    ]
    targets: list[Endpoint | None] = [
        endpoint_from_url(getattr(settings, "database_url", ""), name="postgres", default_port=5432),
        # ⚠️ Redis 是 `required=False`：本项目的 Redis 用途全是**优化**（限流计数、未来的
        # 队列与语义缓存）。它挂了业务照跑（只是限流降级到内存），所以只算「degraded」。
        # Postgres 则相反：它挂了就查不了任何业务数据 —— 那是真的 down。
        endpoint_from_url(getattr(settings, "redis_url", ""), name="redis", default_port=6379, required=False),
    ]
    for target in targets:
        if target is None:
            components.append(
                ComponentHealth(
                    name="unconfigured",
                    status="skipped",
                    detail="未配置连接串（.env 里对应项为空）—— 不算故障，只说明这个依赖没启用",
                )
            )
            continue
        components.append(probe_tcp(target, timeout=timeout))

    names = {
        target.name: target.required for target in targets if target is not None
    }
    required_down = [c for c in components if c.status == "down" and names.get(c.name, False)]
    any_down = [c for c in components if c.status == "down"]
    overall: Literal["ok", "degraded", "down"]
    if required_down:
        overall = "down"
    elif any_down:
        overall = "degraded"
    else:
        overall = "ok"

    return HealthResponse(
        status=overall,
        env=str(getattr(settings, "env", "dev")),
        version=SERVICE_VERSION,
        request_id=str(getattr(request.state, "request_id", "") or ""),
        components=components,
    )


@router.get("/metrics", summary="Prometheus 指标（文本格式 0.0.4）")
def metrics(request: Request) -> Response:
    """把进程内指标渲染成 Prometheus 文本格式。

    ## 第 11 步：为什么渲染**两个**注册表

    | 注册表 | 写者 | 内容 |
    |---|---|---|
    | `app.state.metrics`（按 app 注入） | HTTP 中间件、三条业务路由 | `shopsage_api_*`（QPS/P99/首帧延迟…） |
    | `observability.metrics.default_metrics()` | 图节点、LLM 门面、SQL/检索工具 | `shopsage_agent_*` / `shopsage_llm_*` / `shopsage_sql_*` |

    两者必须是**两个**注册表（前者随 app 注入、后者随进程），
    但抓取端只能配**一个** `/metrics`（否则运维要记得抓两个端点，而漏掉一个的症状是
    「节点级指标怎么没有」——那正是本步要解决的 B1）。所以这里把两段文本拼起来。

    ⚠️ 拼接的前提是**没有重名系列**：若两边有同名指标，Prometheus 抓到的文本里
    会出现重复行，抓取会**部分失败**（`Error on ingesting samples with different value
    but same timestamp`）—— 这是最难查的一类抓取错误。所以 `_registration_collision()`
    在拼接前做一次检查，撞了就让 app 那一份胜出并打一条 ERROR
    （绝不能让「新加的埋点」把已经配好的看板搞坏）。
    """
    registry = getattr(request.app.state, "metrics", None)
    parts = [registry.render()] if registry is not None else ["# 指标注册表未初始化\n"]
    extra = _observability_render(registry)
    if extra:
        parts.append(extra)
    body = "".join(parts)
    # ⚠️ 末尾必须有换行 —— 这是 Prometheus **文本格式**的硬性要求
    # （`version=0.0.4` 的规范：每一行以 LF 结束）。缺了它抓取端会报
    # `unexpected end of stream`，而在浏览器里看完全正常（最迷惑人的一类格式错）。
    # 本地实测踩到：两个注册表拼接时若后一段为空、前一段又被改成不以 LF 结尾，
    # 整体就丢掉了末尾换行。
    if not body.endswith("\n"):
        body += "\n"
    response = Response(content=body, media_type=PROMETHEUS_CONTENT_TYPE)
    response.headers["Cache-Control"] = "no-store"
    return response


def _observability_render(primary: Any) -> str:
    """渲染第 11 步的 telemetry 注册表；**与 primary 同一个就不重复渲染**。"""
    try:
        from app.observability.metrics import default_metrics

        candidate = default_metrics()
    except Exception as exc:  # noqa: BLE001 - 指标不可用不该让抓取端报错
        logger.warning("可观测性指标注册表不可用（已忽略）：%s", exc)
        return ""
    if primary is not None and getattr(primary, "metrics", None) is candidate:
        # 生产路径（`create_app` 把 app 的注册表交给了 telemetry）：同一份，不重复
        return ""
    text = candidate.render()
    if not text.strip():
        return ""
    overlap = _registration_collision(primary, candidate)
    if overlap:
        logger.error(
            "两个指标注册表里有重名系列（已丢弃可观测性那一段，避免抓取端报重复时间戳）：%s",
            sorted(overlap),
        )
        return ""
    return text


def _registration_collision(primary: Any, candidate: Any) -> set[str]:
    """两个注册表之间**是否有重名的指标族**（拼接前必须回答的问题，见 `/metrics`）。"""
    if primary is None:
        return set()
    try:
        left = {metric.name for metric in primary.metrics.registry.collect()}
        right = {metric.name for metric in candidate.registry.collect()}
    except Exception:  # noqa: BLE001 - 检查本身失败时按「不撞」处理（不能因检查而丢数据）
        return set()
    return left & right


__all__: list[Any] = [
    "DEFAULT_PROBE_TIMEOUT",
    "PROMETHEUS_CONTENT_TYPE",
    "SERVICE_VERSION",
    "Endpoint",
    "endpoint_from_url",
    "observability_component",
    "probe_tcp",
    "rate_limit_component",
    "router",
]
