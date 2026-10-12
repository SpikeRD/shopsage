"""`tests/api/` 的局部装置：**装配一个带假模型、内存 checkpointer、小额度限流的 app**。

与 `tests/graph/conftest.py` 的分工：那边管「图这一层的事件循环策略」，
这边管「API 这一层的依赖注入」。两者都需要 `ensure_compatible_event_loop()`，
但只有图相关目录需要把它放在 import 期 —— API 用例的图是内存 checkpointer（不碰 psycopg），
所以这里在 import 期也调一次只是「保持一致、避免顺序依赖」。

## 为什么这些装置不放在顶层 `tests/conftest.py`

`tests/api/` 的用例都要 `TestClient(app)`，而 app 的装配要读 `.env`（限流后端可能会去连 Redis）。
把这种装置放到顶层，会让**其余 700+ 条既有用例**在收集期就多出「连 Redis 的可能」——
那是典型的「为了一条用例改变全体环境」。

## 四条纪律（每个 fixture 都对应其中一条）

1. **不连真库**：`patched_query` 换掉 `data` 节点的 SQL 执行器（与 `tests/graph/*` 同口径）。
2. **不连真 Redis**：限流注入 `MemoryRateLimitStore`。
3. **不花真钱**：`AgentFakeLLM` 按 `agent=` 分派回执，没人调真模型。
4. **不污染别的用例**：`get_settings()` 的 lru_cache 由顶层 conftest 清；
   这里额外保证「每个 app 有独立的 metrics 与限流计数」。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.api.metrics import MetricsRegistry
from app.api.ratelimit import MemoryRateLimitStore, RateLimitRule, RateLimiter
from app.graph import GraphRuntime, ensure_compatible_event_loop
from app.security import auth

#: 与 `tests/graph/*` 同口径：先把 Windows 的事件循环策略摆正（必须早于任何循环创建）
ensure_compatible_event_loop()

#: 单条用例的看门狗上限（秒）。超时就把整条用例判失败并**打印所有线程的栈**。
#:
#: 为什么需要它（本仓实测的教训）：`TestClient` 的 WebSocket 是**线程 + anyio portal** 实现的，
#   一旦出现「服务端等客户端、客户端等服务端」的错配，`pytest` 会**永久挂住** ——
#   没有输出、没有失败、CI 上只能看到超时。挂住 5 分钟和红一条用例，
#   对排障的价值差一个数量级：后者会告诉你卡在哪一行。
WS_HANG_TIMEOUT_SECONDS = 30.0

#: 用例里的标准 SQL（假模型回执与假执行器都用它，避免两处各写一份）
SQL = "SELECT order_id, status FROM orders WHERE user_id = :uid LIMIT :n"
ROWS = [{"order_id": 1024, "status": "shipped"}, {"order_id": 1025, "status": "pending"}]

#: 一条完整 `data` 路径的假模型回执（router / data / synthesis / guardrail 四份）
DATA_REPLIES: dict[str, Any] = {
    "router": {"route": "data", "certainty": 0.95, "reason": "问自己的订单"},
    "data": {"sql": SQL, "params": {"uid": 999, "n": 10}, "reason": "查订单"},
    "synthesis": {"answer": "您最近 2 笔订单：1 笔已发货、1 笔待付款。", "cited": []},
    "guardrail": {"allowed": True, "reason": "ok"},
}


@pytest.fixture(autouse=True)
def _watchdog() -> Iterator[None]:
    """给每条用例装一个看门狗：超时就**打印所有线程的栈并让用例失败**。

    理由见 `WS_HANG_TIMEOUT_SECONDS` 的注释。实现上是一个 daemon 线程 + `faulthandler`：
    它不改任何业务行为，只保证「卡住」这种故障在这里是可诊断的。
    """
    import faulthandler
    import sys
    import threading

    done = threading.Event()

    def watch() -> None:
        if done.wait(WS_HANG_TIMEOUT_SECONDS):
            return
        sys.stderr.write(
            f"\n[看门狗] 用例超过 {WS_HANG_TIMEOUT_SECONDS}s 未结束，打印所有线程的栈：\n"
        )
        sys.stderr.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()

    thread = threading.Thread(target=watch, name="api-test-watchdog", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()


@pytest.fixture
def patched_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """把真库执行器换掉（离线）。**只**换这一个出口，其余全是真实现。"""
    from app.agents import data as data_mod
    from tests.graph_helpers import fake_query_result

    monkeypatch.setattr(data_mod, "execute_readonly_query", fake_query_result(ROWS))


@pytest.fixture
def fake_llm() -> Any:
    """一条 `data` 路径的假 LLM（用例可以改它的 `replies`）。"""
    from tests.graph_helpers import AgentFakeLLM

    return AgentFakeLLM(**DATA_REPLIES)


@pytest.fixture
def metrics() -> MetricsRegistry:
    return MetricsRegistry()


def tiny_limiter(*, chat: int = 3, stream: int = 3, approve: int = 3) -> RateLimiter:
    """小额度限流器（验 429 用）。

    ⚠️ 窗口给 60 秒（而不是 0.1 秒）：用例要断言的是「超限即 429」，
    窗口短了会让「第 4 次请求时第 1 次刚好滑出窗口」变成偶发失败（flaky）——
    这是本仓最讨厌的一类用例（它在 CI 上红、在本地绿）。
    """
    return RateLimiter(
        store=MemoryRateLimitStore(),
        rules={
            "chat": RateLimitRule(limit=chat, window_seconds=60.0),
            "chat.stream": RateLimitRule(limit=stream, window_seconds=60.0),
            "chat.approve": RateLimitRule(limit=approve, window_seconds=60.0),
        },
    )


@pytest.fixture
def queue() -> Any:
    """第 12 步的内存队列：让 `POST /api/tasks` 的契约能**离线**验完。

    ⚠️ 默认就注入它（不注入的话 `create_app` 会去连 `REDIS_URL`）——
    与「默认注入假模型」是同一条纪律：测试不该依赖（也不该影响）真实依赖栈。
    """
    from tests.queue.fake_queue import MemoryJobQueue

    return MemoryJobQueue()


@pytest.fixture
def app_factory(metrics: MetricsRegistry, queue: Any) -> Callable[..., Any]:
    """造 app 的工厂（用例可以按需改限流/模型/prefix）。

    ⚠️ **默认就注入假模型**（`llm=None` 会落到生产出口 `app.llm.llm_call`）。
    这条不是「方便」，是纪律：本仓的 `.env` 里有一把真 key，忘了注入假模型的用例会
    **真的去打厂商 API** —— 它照样能过（拿到一段真回答），但每次回归都在花钱，
    而且断言会随模型措辞漂移（本文件第一版就踩过：一条断言 `answer.startswith("您最近 2 笔订单")`
    的用例拿到了真实模型的回答，红得让人以为是流式协议坏了）。
    """

    def build(**overrides: Any) -> Any:
        from tests.graph_helpers import AgentFakeLLM, memory_saver

        params: dict[str, Any] = {
            "runtime": overrides.pop(
                "runtime",
                GraphRuntime(
                    llm=overrides.pop("llm", None) or AgentFakeLLM(**DATA_REPLIES),
                    search=overrides.pop("search", lambda **_: []),
                ),
            ),
            "rate_limiter": overrides.pop("rate_limiter", tiny_limiter()),
            "metrics": metrics,
            # 第 12 步：注入内存队列（理由见 `queue` 装置）
            "queue": overrides.pop("queue", queue),
            # 用注入的工厂而不是 `backend="memory"`：走的是与生产**同一条**路径
            # （`factory()` 返回 `async with` 上下文），只是后端换成内存。
            "checkpointer_factory": overrides.pop(
                "checkpointer_factory",
                lambda: _context(memory_saver()),
            ),
            # 探活不连真依赖：用例只需要一个确定的结果
            "probe_timeout_value": overrides.pop("probe_timeout_value", 0.05),
        }
        # 传了 `runtime` 的用例自己负责模型注入，这里把剩下的 `llm=`/`search=` 吃掉
        # （否则它们会变成 `create_app` 的不认识参数）
        overrides.pop("llm", None)
        overrides.pop("search", None)
        params.update(overrides)
        return create_app(**params)

    return build


def _context(saver: Any) -> Any:
    """把 saver 包成 `async with` 上下文（与 `open_checkpointer` 的返回形状一致）。"""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm() -> Any:
        yield saver

    return _cm()


@pytest.fixture
def app(app_factory: Callable[..., Any]) -> Any:
    return app_factory()


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    """`TestClient` **必须用 `with`**：`app.state.graph_factory` 在 lifespan 里才装好。"""
    with TestClient(app) as test_client:
        yield test_client


def make_token(user_id: int = 2, role: str = "customer", **claims: Any) -> str:
    return auth.create_access_token(user_id, role=role, **claims)


@pytest.fixture
def customer_token() -> str:
    """客户身份（user_id=2）。**行级过滤的依据就是这个值**，所以用例里要显式写出来。"""
    return make_token(2, "customer")


@pytest.fixture
def internal_token() -> str:
    """内部员工身份（user_id=7）：有 `read:all_orders` 权限 → `internal=True`。"""
    return make_token(7, "internal_employee")


@pytest.fixture
def auth_headers(customer_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {customer_token}"}


def headers_for(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ws_subprotocols(token: str) -> list[str]:
    """浏览器可用的 WebSocket 子协议：`shopsage.v1` + `bearer.<token>`。"""
    from app.api.schemas import WS_SUBPROTOCOL, WS_TOKEN_PREFIX

    return [WS_SUBPROTOCOL, f"{WS_TOKEN_PREFIX}{token}"]


def collect_frames(websocket: Any, *, max_frames: int = 20) -> list[dict[str, Any]]:
    """把一条连接上的帧收齐（直到服务端关闭或达到上限）。

    ⚠️ 终止条件是 `WebSocketDisconnect` 异常：`TestClient` 的 `receive_json()` 在连接被
    服务端关闭时**抛异常**（而不是返回一条 close 消息）。用例里自己写这个循环的结果是
    「有的用例漏读到最后一帧、有的用例卡在等待」—— 两种都很难查，所以收成一处。
    """
    from starlette.websockets import WebSocketDisconnect

    frames: list[dict[str, Any]] = []
    for _ in range(max_frames):
        try:
            frames.append(websocket.receive_json())
        except WebSocketDisconnect:  # pragma: no cover - 正常路径下不会走到（服务端不主动关）
            break
        except Exception:  # noqa: BLE001 - 连接结束
            break
    return frames


def frames_by_event(frames: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [frame for frame in frames if frame.get("event") == event]


def frame_text(frames: list[dict[str, Any]], event: str) -> str:
    for frame in frames:
        if frame.get("event") == event:
            return str(frame.get("data", {}).get("text") or "")
    return ""


def read_until_done(websocket: Any, *, max_frames: int = 40) -> list[dict[str, Any]]:
    """一直读到 `done` 帧为止（一轮流式问答的终止信号）。

    ⚠️ 为什么不用「固定读 N 帧」：N 会随图的节点数变化（今天 4 个节点 → 11 帧，
    第 10 步补上 rag、第 11 步给闸门加节点之后就不一样了）。写死 N 的用例会在
    **别的步骤**的改动里莫名其妙地红，而那时没人会想到是这里。
    """
    frames: list[dict[str, Any]] = []
    for _ in range(max_frames):
        try:
            frame = websocket.receive_json()
        except Exception:  # noqa: BLE001 - 连接结束：把已收到的返回，由调用方断言
            break
        frames.append(frame)
        if frame.get("event") == "done":
            break
    return frames


def wait_until_closed(websocket: Any) -> int:
    """读帧直到连接关闭，返回关闭码；**超时返回 0**（这是一个断言依据，不是失败）。

    ⚠️ 不能用「一直 `receive_text()` 等异常」的写法：那在「服务端已经关闭但客户端还在等」
    的情况下会**永久挂住**（实测：`pytest -q tests/api/test_rate_limit.py` 直接卡死到超时）。
    正确姿势是**读帧**（读到 close 帧就返回），并给它一个上限 ——
    用例宁可红得清楚，也不要挂得不明不白。
    """
    from starlette.websockets import WebSocketDisconnect

    for _ in range(16):
        try:
            frame = websocket.receive()
        except WebSocketDisconnect as exc:
            return int(getattr(exc, "code", 1000) or 1000)
        except Exception:  # noqa: BLE001 - 连接结束（各种形态）
            return 1000
        if frame.get("type") == "websocket.close":
            return int(frame.get("code", 1000))
    return 0


__all__ = [
    "DATA_REPLIES",
    "ROWS",
    "SQL",
    "collect_frames",
    "frame_text",
    "frames_by_event",
    "headers_for",
    "make_token",
    "read_until_done",
    "tiny_limiter",
    "wait_until_closed",
    "ws_subprotocols",
]
