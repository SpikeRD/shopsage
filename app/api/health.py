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


@router.get("/health", response_model=HealthResponse, summary="健康检查（依赖状态在 body 里）")
def health(request: Request) -> HealthResponse:
    """探活：**恒 200**，依赖状态写在 `components`（理由见模块 docstring）。"""
    settings = getattr(request.app.state, "settings", None)
    timeout = float(getattr(request.app.state, "probe_timeout", None) or DEFAULT_PROBE_TIMEOUT)

    components: list[ComponentHealth] = [rate_limit_component(request)]
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

    `MetricsRegistry` 由 `main.create_app` 挂在 `app.state.metrics` 上；
    取不到就返回一段空输出（而不是 500）—— 指标不可用不该让抓取端报错，
    那会把「指标系统的小问题」升级成「业务接口故障」的告警。
    """
    registry = getattr(request.app.state, "metrics", None)
    body = registry.render() if registry is not None else "# 指标注册表未初始化\n"
    response = Response(content=body, media_type=PROMETHEUS_CONTENT_TYPE)
    response.headers["Cache-Control"] = "no-store"
    return response


__all__: list[Any] = [
    "DEFAULT_PROBE_TIMEOUT",
    "PROMETHEUS_CONTENT_TYPE",
    "SERVICE_VERSION",
    "Endpoint",
    "endpoint_from_url",
    "probe_tcp",
    "rate_limit_component",
    "router",
]
