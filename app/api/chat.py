"""`POST /api/chat`、`WS /api/chat/stream`、`POST /api/chat/approve`（`9.api.md` §2、§6 第 2 步）。

本模块**只做四件事**（`9.api.md` §4 第 4 条：API 层无业务逻辑）：

```
鉴权 → 校验 → 转交图 → 格式化返回
```

业务全在 `app/graph` 与 `app/agents`；本模块里**不允许**出现 SQL、prompt、脱敏规则、权限判断
（权限只用来决定「调试载荷给不给」，那属于输出裁剪，不是授权）。

## 流式：先出骨架，再补细节（`9.api.md` §4 第 2 条、B2）

帧的时序（实测）：

```
t≈0ms   {"event":"skeleton"}                          ← 握手后立刻发，不含任何模型产物
t≈?     {"event":"node","data":{"node":"guardrail_input"}}
t≈?     {"event":"node","data":{"node":"router"}}
        {"event":"skeleton","data":{"phase":"routing","route":"data","text":"…"}}  ← 骨架按 route 收敛
t≈?     {"event":"node","data":{"node":"data","delta":{"route":"data",…}}}
t≈?     {"event":"node","data":{"node":"synthesis","delta":{"answer":"您最近 2 笔订单…"}}}
        {"event":"answer","data":{"text":"…","done":true,…}}
t≈?     {"event":"done","data":{"flow_state":"delivered","latency_ms":…,"first_frame_ms":…}}
```

⚠️ **诚实的边界：`synthesis` 目前不是逐 token 产出的**。
`app/agents/agent_llm.generate_json` 走的是「整段 JSON 一次返回」（结构化输出要能整体解析），
所以 `answer` 帧里的 `text` 是**完整回答**，不是增量。这一点刻意不粉饰：
`answer` 帧带 `"done": true`，前端据此知道「不用等下一帧补字」；
而 `skeleton` 帧（首个字节，实测毫秒级）已经给了骨架与进度，B2 的「<1s」由它保证。
逐 token 的增量帧是第 11 步「带流式的合成」要补的活 —— 届时**只需在 `answer` 帧之前
多发若干增量帧**，协议不用改（前端按 `event` 分支），所以这个边界不会变成返工。

## 断开的连接：状态不由连接持有

客户端中途断开时，**图会继续跑完**（服务端不取消它）：这一轮的结果会落进 checkpoint，
用户回来用同一个 `session_id` 再问一次时，历史是完整的。
反过来若在断开时取消图，checkpoint 会停在半路 —— 而「半路的会话」比「多花一次模型调用」贵得多。

## 为什么批准通道要单独一个接口

敏感操作挂起时（`flow_state="approval_required"`），响应里给了 `thread_id` 与 `question`；
批准必须**另发一次请求**（而不是「连接上回一句 yes」）：

* 批准人可能不是发起人（客服/主管代批），他要走的是另一条鉴权路径；
* 批准必须留审计（谁批的、什么时候、理由）—— 塞进流里就没有落点了；
* 挂起的状态在 checkpoint 里，与连接无关，所以「换一个连接来批准」是天然支持的。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Final

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, status

from app.api import deps
from app.api.errors import ApiError
from app.api.ratelimit import caller_bucket
from app.api.scope import WS_SCOPE_CHAT_STREAM
from app.api.schemas import (
    FRAME_ANSWER,
    FRAME_DONE,
    FRAME_ERROR,
    FRAME_NODE,
    FRAME_SKELETON,
    WS_CLOSE_RATE_LIMITED,
    WS_CLOSE_UNAUTHORIZED,
    WS_SUBPROTOCOL,
    ApprovalDecision,
    ApprovalGate,
    ChatDebug,
    ChatRequest,
    ChatResponse,
)
from app.graph import graph_config, interrupt_policy_for, run_turn
from app.graph.config import APPROVAL_QUESTION, REJECTED_REASON
from app.observability.instrument import trace_request_span
from app.security.auth import AuthError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

#: 骨架话术：**按 route 给一句「我正在做什么」**，不含任何需要模型产出的内容。
#:
#: 为什么要分 route：`skeleton` 帧的价值就是「让用户知道系统理解了他的问题」。
#: 如果所有 route 都给同一句「正在处理」，用户看不出「它是要查我的订单、还是去翻政策」——
#: 那就只是一句「加载中」，不是「结论骨架」（`9.api.md` §4 第 2 条要的是后者）。
SKELETONS: Final[dict[str, str]] = {
    "": "收到，正在理解你的问题…",
    "routing": "已理解你的问题，正在准备结论…",
    "data": "正在查询你的业务数据，结论马上给你…",
    "rag": "正在检索知识库并核对出处…",
    "analysis": "正在汇总数据并做趋势判断…",
    "ticket": "这属于需要人工确认的操作，正在准备审批…",
    "final": "正在整理回答…",
}

#: 节点名 → 给用户看的进度文案（**不含内部细节**：不暴露 SQL、不暴露检索参数）
NODE_PROGRESS: Final[dict[str, str]] = {
    "guardrail_input": "正在做安全校验…",
    "router": "正在判断你的意图…",
    "data": "正在查询数据…",
    "rag": "正在检索知识库…",
    "analysis": "正在做数据分析…",
    "gate": "等待人工确认…",
    "synthesis": "正在组织回答…",
    "guardrail_output": "正在做输出合规检查…",
}

#: `node` 帧里**允许**出网的增量键。
#:
#: ⚠️ 这是一个**白名单**，不是黑名单。`stream_mode="updates"` 的增量里可能带
#: `rows`（原始查询结果，可能含他人数据/PII）、`permissions`、`messages`（完整对话历史）……
#: 用「排除几个敏感键」的写法，新增状态键时会**默认泄漏**；白名单则默认安全
#: （新键要么被显式加进来，要么不出网）。这与 `4.security.md` 的表/列白名单是同一条纪律。
FRAME_SAFE_KEYS: Final[tuple[str, ...]] = (
    "route",
    "route_confidence",
    "answer",
    "citations",
    "blocked",
    "guardrail_stage",
    "refusals",
    "chart_spec",
)


# ── 输出裁剪与装配 ───────────────────────────────────────────────────────────


def shape_debug(turn: Any, *, settings: Any = None, internal: bool = False) -> ChatDebug | None:
    """造调试载荷；**对客默认整块不返回**（见 `schemas.ChatDebug` 的说明）。

    为什么给 `internal` 角色也要看 `API_DEBUG_PAYLOAD`：内部角色里也可能有「页面直接展示」
    的场景，而 `visited` 这种字段对最终用户没有意义。两个条件都满足才给，
    等于「内部 + 显式开启」，比「是内部人就给」多一道人为确认。
    """
    from app.api.config import debug_payload_enabled

    if not (internal and debug_payload_enabled(settings)):
        return None
    state = dict(getattr(turn, "state", None) or {})
    meta = dict(state.get("row_meta") or {})
    return ChatDebug(
        visited=list(getattr(turn, "visited", None) or []),
        replayed=list(getattr(turn, "replayed", None) or []),
        exhausted=bool(getattr(turn, "exhausted", False)),
        guardrail_stage=str(state.get("guardrail_stage") or ""),
        guardrail_rules=list(state.get("guardrail_rules") or []),
        row_count=meta.get("row_count"),
        truncated=meta.get("truncated"),
        sql=str(state.get("sql") or "") or None,
    )


def approval_gate(turn: Any) -> ApprovalGate | None:
    """挂起时造审批信息（`question` 优先取闸门载荷里的，取不到用图的固定话术）。"""
    payload = dict(getattr(turn, "interrupt", None) or {})
    if not payload:
        return None
    return ApprovalGate(
        thread_id=str(turn.thread_id),
        question=str(payload.get("question") or APPROVAL_QUESTION),
        interrupt_id=str(payload.get("interrupt_id") or ""),
    )


def build_response(turn: Any, *, actor: deps.Actor, settings: Any = None) -> ChatResponse:
    """把 `Turn` 翻成响应信封（**唯一的翻译点**：HTTP 响应与 WS 的 `done` 帧共用）。"""
    state = dict(getattr(turn, "state", None) or {})
    gate = approval_gate(turn)
    if gate is not None:
        flow = "approval_required"
    elif turn.blocked:
        flow = "blocked"
    else:
        flow = "delivered"
    return ChatResponse(
        answer=turn.answer,
        flow_state=flow,  # type: ignore[arg-type]
        thread_id=turn.thread_id,
        session_id=actor.session_id,
        request_id=actor.request_id,
        route=str(state.get("route") or ""),
        blocked=bool(turn.blocked),
        citations=[str(item) for item in (state.get("citations") or [])],
        refusals=[str(item) for item in (state.get("refusals") or [])],
        chart_spec=state.get("chart_spec") or None,
        approval=gate,
        debug=shape_debug(turn, settings=settings, internal=actor.internal),
    )


def entry_state(actor: deps.Actor, message: str) -> dict[str, Any]:
    """造图的入口 state（**与 `tests/graph_helpers.customer_state` 同口径**）。

    `user_id` / `role` 来自**已验签的令牌**（`inject_identity` 从 `rbac` 查表得权限点）；
    请求体里没有、也不接受这两个字段（那会是 D1 最经典的入口）。
    """
    return {"messages": [{"role": "user", "content": str(message)}], **actor.state_fields()}


def safe_delta(update: Any) -> dict[str, Any]:
    """裁剪节点增量：只保留 `FRAME_SAFE_KEYS`，并把列表截断到可传输的长度。"""
    if not isinstance(update, dict):
        return {}
    out: dict[str, Any] = {}
    for key in FRAME_SAFE_KEYS:
        if key not in update:
            continue
        value = update[key]
        if key == "chart_spec":
            if value:
                out["chart"] = True
            continue
        if isinstance(value, (list, tuple)):
            out[key] = [str(item) for item in value][:10]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out


# ── HTTP：非流式问答 ─────────────────────────────────────────────────────────


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="非流式问答（鉴权 → 转交图 → 返回结论）",
)
async def chat(
    body: ChatRequest,
    request: Request,
    principal: Any = Depends(deps.require_principal),
) -> Any:
    """一轮问答：`POST /api/chat`（`9.api.md` §6 第 2 步）。

    ⚠️ 参数顺序**不是随意的**：`body` 在 `principal` 之前，于是**请求体校验先于鉴权**
    （FastAPI 先解析参数、再跑依赖）。三个后果都是想要的：

    * 未带令牌 + 请求体不合规 → 422（而不是 401）：直接告诉调用方「请求形状不对」，
      比「先要凭证、给了凭证再说你字段写错了」少一轮往返，且不泄漏任何东西；
    * 未带令牌 + 请求体合规 → 401（鉴权仍是准入条件）；
    * **限流在最外层**（中间件），所以「未鉴权的暴力请求」仍然先被 429 挡住 ——
      「先 422」不会变成一条绕过限流的通道。

    返回码的三种情况（`flow_state` 见 `schemas` 模块 docstring 的表）：

    | 情况 | 码 | 说明 |
    |---|---|---|
    | 正常答完 | 200 | `flow_state="delivered"` |
    | 被拦/兜底 | 200 | `flow_state="blocked"`：`answer` 里是安全话术或如实说明 |
    | 挂起等批准 | **202** | `flow_state="approval_required"`，带 `thread_id` 与问题 |
    """
    session_id = deps.resolve_session_id(body.session_id)
    actor = deps.actor_from_principal(
        principal,
        session_id=session_id,
        request_id=str(getattr(request.state, "request_id", "")),
    )
    settings = getattr(request.app.state, "settings", None)
    metrics = getattr(request.app.state, "metrics", None)

    started = time.perf_counter()
    turn = await run_turn(
        _graph_for(request.app, actor),
        entry_state(actor, body.message),
        thread_id=actor.thread_id,
    )
    elapsed = time.perf_counter() - started

    response = build_response(turn, actor=actor, settings=settings)
    if metrics is not None:
        # ⚠️ 标签用**最终**的 flow_state（不是 "pending"）：指标的价值在于能按结局分组，
        # 一个永远等于 "pending" 的标签比没有标签更糟（它会让人以为统计坏了）。
        metrics.counter(
            "shopsage_api_chat_turns_total",
            {"endpoint": "POST /api/chat", "flow_state": response.flow_state},
        )
        metrics.observe("shopsage_api_chat_turn_seconds", elapsed, {"endpoint": "POST /api/chat"})
        metrics.counter("shopsage_api_chat_route_total", {"route": response.route or "unknown"})
    logger.info(
        "chat 完成：thread=%s flow=%s route=%s 耗时=%.0fms 路径=%s",
        actor.thread_id,
        response.flow_state,
        response.route,
        elapsed * 1000,
        "→".join(turn.visited),
    )
    if response.flow_state == "approval_required":
        return _accepted(response, request_id=actor.request_id)
    return response


def _accepted(model: ChatResponse, *, request_id: str) -> Any:
    """用 202 返回同一个信封（`response_model` 描述不了「有时 200 有时 202」）。"""
    from fastapi.responses import JSONResponse

    from app.api.schemas import REQUEST_ID_HEADER

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=model.model_dump(mode="json"),
        headers={REQUEST_ID_HEADER: request_id} if request_id else None,
    )


def _graph_for(app: Any, actor: deps.Actor) -> Any:
    """取本次请求要用的图：**按角色决定是否开人工审批闸门**。

    闸门规则在 `app/graph/config.py`（对客开、内部关）—— API 层只负责**问**它，
    不负责定义它。`_runtime_with_policy` 返回**新的 `GraphRuntime`**（`dataclasses.replace`）：
    `app.state.graph_runtime` 是所有请求共享的实例，就地改它的 `interrupts`
    会让「一个客户的请求」改变「另一个内部员工请求」的行为 —— 那是最难查的一类并发 bug。

    ⚠️ 参数是 **ASGI app**（`request.app` / `websocket.app`）而不是 `request`：
    HTTP 与 WebSocket 都要用这一个函数，而 `WebSocket` 没有 `.app.state` 这种形状
    （`WebSocket.state` 指的是「这条连接的状态」—— 同名不同物）。实测把 `websocket`
    当 request 传进来会得到 `AttributeError: 'State' object has no attribute 'state'`。
    """
    factory = app.state.graph_factory
    runtime = _runtime_with_policy(app.state.graph_runtime, actor)
    return factory(runtime, actor)


def _runtime_with_policy(runtime: Any, actor: deps.Actor) -> Any:
    """把「这个角色要不要过闸门」算进 runtime（不改共享实例，见 `_graph_for`）。"""
    import dataclasses

    policy = interrupt_policy_for(actor.role)
    if getattr(runtime, "interrupts", None) == policy:
        return runtime
    return dataclasses.replace(runtime, interrupts=policy)


# ── HTTP：人工审批 ───────────────────────────────────────────────────────────


@router.post(
    "/chat/approve",
    response_model=ChatResponse,
    summary="人工审批：批准/拒绝挂起的那一轮（human-in-the-loop）",
)
async def approve(
    body: ApprovalDecision,
    request: Request,
    principal: Any = Depends(deps.require_principal),
) -> ChatResponse:
    """把挂起的那一轮**从 checkpoint 接着跑**（`8.graph.md` §5 第四条）。

    四件事的顺序不能换（每一件都有它防的东西）：

    1. **校验 `thread_id` 属于当前用户**：否则任何人都能批准（甚至推进）别人的会话
       —— 这与「任选 `thread_id` 读别人状态」是同一个洞的另一面；
    2. **确认这一轮确实在等批准**：拿 `aget_state().next` 看有没有挂起的节点。
       没有挂起却去 `Command(resume=...)`，LangGraph 的语义是**从头重跑** ——
       用户会拿到一句莫名其妙的新回答，而上游已经花掉的模型调用再花一遍；
    3. 用 `Command(resume=<决策>)` 恢复。`resume` 的值就是闸门节点里 `interrupt()` 的返回值 ——
       本项目传一个 dict（`approved` / `reason` / `approver_user_id` / `approver_role`），
       让将来的写节点能拿到「谁批的、批了没、为什么」（审计要这四个）；
    4. 未批准时**保证**有一句如实的话术（在这里补一道，不依赖闸门实现里那行 `refusals`）。
    """
    from langgraph.types import Command

    actor = deps.actor_from_principal(
        principal,
        session_id=deps.restore_session_id(body.thread_id, user_id=principal.user_id),
        request_id=str(getattr(request.state, "request_id", "")),
    )
    if actor.thread_id != body.thread_id:
        raise ApiError(
            422,
            "thread_mismatch",
            "thread_id 与当前用户派生的会话不一致",
            detail={"expected": actor.thread_id},
        )

    graph = _graph_for(request.app, actor)
    snapshot = await graph.aget_state(graph_config(actor.thread_id))
    if not tuple(snapshot.next or ()):
        raise ApiError(
            409,
            "nothing_to_approve",
            "这个会话没有等待批准的敏感操作（可能已被批准、已过期，或它根本不需要批准）",
            detail={"thread_id": actor.thread_id},
        )

    turn = await run_turn(
        graph,
        Command(
            resume={
                "approved": bool(body.approved),
                "reason": str(body.reason or ""),
                "approver_user_id": int(principal.user_id),
                "approver_role": str(principal.role),
            }
        ),
        thread_id=actor.thread_id,
    )
    if not body.approved and not turn.answer:
        turn.state["answer"] = REJECTED_REASON
        turn.state["blocked"] = True
    logger.info(
        "审批完成：thread=%s approved=%s approver=%s flow=%s",
        actor.thread_id,
        body.approved,
        principal.user_id,
        "blocked" if turn.blocked else "delivered",
    )
    return build_response(turn, actor=actor, settings=getattr(request.app.state, "settings", None))


# ── WebSocket：流式问答 ──────────────────────────────────────────────────────


@router.websocket("/chat/stream")
async def chat_stream(websocket: WebSocket) -> None:
    """`WS /api/chat/stream`（B2：先出结论骨架，再补细节）。

    握手顺序（**每一段的顺序都有理由**）：

    1. **先取子协议与令牌、再 accept**：未鉴权的连接不该被建立。
       但要给客户端一个**能读懂的原因**，所以 accept 之后发一条 `error` 帧再关，
       关闭码用 4401（应用自定义段）—— 浏览器拿不到握手阶段的 HTTP 状态码，
       关闭码是它唯一能编程处理的信息。
    2. `accept(subprotocol="shopsage.v1")`：浏览器要求服务端选的值必须是它提出的之一，
       所以客户端**必须**同时带 `shopsage.v1` 与 `bearer.<token>`。
    3. 限流：在 accept 之后判定（这样 429 能作为 `error` 帧 + 4429 关闭码送达，
       而不是一个「连接莫名被拒」）。
    4. 进入消息循环：每条消息一轮问答，**连接不断开**（多轮对话复用一条连接）。
    """
    subprotocols = deps.ws_subprotocols(websocket)
    try:
        principal = deps.ws_actor(websocket)
    except Exception as exc:  # noqa: BLE001 - 缺令牌/子协议都算鉴权失败
        if not isinstance(exc, AuthError):
            logger.warning("WS 鉴权异常：%s", exc)
        await websocket.accept(subprotocol=subprotocols[0] if subprotocols else None)
        await _send_frame(
            websocket,
            1,
            FRAME_ERROR,
            {"code": "unauthorized", "message": str(exc) or "鉴权失败"},
        )
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept(subprotocol=WS_SUBPROTOCOL if subprotocols else None)

    app_state = websocket.app.state
    limiter = getattr(app_state, "rate_limiter", None)
    caller = caller_bucket(user_id=principal.user_id, role=principal.role)
    if limiter is not None:
        decision = limiter.check(caller, WS_SCOPE_CHAT_STREAM)
        if not decision.allowed:
            await _send_frame(
                websocket,
                1,
                FRAME_ERROR,
                {
                    "code": "rate_limited",
                    "message": f"请求过于频繁：每 {int(decision.window_seconds)} 秒最多 {decision.limit} 次",
                    "retry_after": decision.retry_after,
                },
            )
            await websocket.close(code=WS_CLOSE_RATE_LIMITED)
            return

    seq = 0
    # ── 第 11 步：WS 的根 span（**中间件只管 HTTP，WS 要自己开**）──────────────
    #
    # ⚠️ 为什么 WebSocket 必须单独处理：`ApiMiddleware` 在 `scope["type"] != "http"`
    # 时直接放行（WS 的限流在路由里做，那里才有身份）—— 于是 WS 请求**没有根 span**，
    # 而 WS 正是 B2 的主入口（`WS /api/chat/stream`）：一次连接里的每一轮问答
    # 都会各自成为一棵「没有根的树」。后果与其他路径上的同一个坑一样：
    # 「按 trace 查这次流式问答」失效，而每个节点看起来都埋了点。
    #
    # ⚠️ 根 span 包住**整个连接的消息循环**（而不是每一轮）：一条 WS 连接是
    # 多轮对话共用的（见模块 docstring），所以「连接」才是那个「一次请求」。
    # 想按轮区分就看 `node.*` span 的时间顺序 —— 它们天然按轮分簇。
    with trace_request_span("WS", "/api/chat/stream"):
        while True:
            try:
                # ⚠️ `mode="text"` **必须显式给**：Starlette 1.7 的 `receive_json()` 默认
                # `mode="text"`，但它内部用 `message["text"]` 取值；反过来 `mode="binary"` 才会
                # 去取 `message["bytes"]`。显式写出来是为了让「我们只收文本帧」这条约定
                # 出现在代码里 —— 客户端发二进制帧时会得到一条 `bad_frame` 错误帧而不是静默卡住。
                raw = await websocket.receive_json(mode="text")
            except WebSocketDisconnect:
                logger.debug("WS 客户端断开（user=%s）", principal.user_id)
                return
            except json.JSONDecodeError:
                seq += 1
                await _send_frame(websocket, seq, FRAME_ERROR, {"code": "bad_json", "message": "消息不是合法 JSON"})
                continue
            except Exception as exc:  # noqa: BLE001 - 客户端发来非文本帧等
                seq += 1
                await _send_frame(websocket, seq, FRAME_ERROR, {"code": "bad_frame", "message": str(exc)})
                continue

            try:
                session_id = deps.resolve_session_id(raw.get("session_id"))
                message = str(raw.get("message") or "").strip()
                if not message:
                    raise ApiError(422, "empty_message", "message 不能为空")
                if len(message) > 2000:
                    raise ApiError(422, "message_too_long", f"message 长度上限 2000 字（收到 {len(message)}）")
            except ApiError as exc:
                seq += 1
                await _send_frame(websocket, seq, FRAME_ERROR, {"code": exc.code, "message": exc.message})
                continue

            actor = deps.actor_from_principal(
                principal,
                session_id=session_id,
                request_id=str(getattr(websocket.state, "request_id", "")),
            )
            seq = await _stream_turn(
                websocket,
                actor=actor,
                message=message,
                seq=seq,
                settings=getattr(app_state, "settings", None),
                metrics=getattr(app_state, "metrics", None),
            )


async def _stream_turn(
    websocket: WebSocket,
    *,
    actor: deps.Actor,
    message: str,
    seq: int,
    settings: Any,
    metrics: Any,
) -> int:
    """跑一轮并把增量推成帧；返回新的 `seq`。

    ⚠️ 顺序是「先发骨架 → 再跑图 → 补节点帧 → 发回答」。
    「跑图」必须在「补节点帧」之前，因为 `run_turn` 的 `on_event` 是**同步回调**
    （图在事件循环里同步地流式产出增量，回调里 `await` 不了）——
    所以回调用列表记帧、跑完再按序发。代价是「节点帧不是实时到达」，
    换来的是「不用为了推送而把图拆成两半」（拆开的代价远大于这点延迟：
    `run_turn` 的重放与兜底逻辑会一起被拆散）。
    """
    app_state = websocket.app.state
    graph = _graph_for(websocket.app, actor)
    started = time.perf_counter()

    # ── ① 骨架帧：**握手后立刻发**（B2 的首字节在这里，不含任何模型产物）────────
    seq += 1
    await _send_frame(websocket, seq, FRAME_SKELETON, {"phase": "accepted", "text": SKELETONS[""]})
    first_frame_ms = (time.perf_counter() - started) * 1000

    frames: list[dict[str, Any]] = []

    def on_event(node: str, update: Any) -> None:
        frames.append({"node": str(node), "update": update})

    turn = await run_turn(
        graph,
        entry_state(actor, message),
        thread_id=actor.thread_id,
        on_event=on_event,
    )

    # ── ② 节点帧：按顺序补发（含「骨架按 route 收敛」那一条）────────────────────
    sent_route = ""
    for item in frames:
        node = item["node"]
        delta = safe_delta(item["update"])
        seq += 1
        await _send_frame(
            websocket,
            seq,
            FRAME_NODE,
            {"node": node, "progress": NODE_PROGRESS.get(node, ""), "delta": delta},
        )
        route = str(delta.get("route") or "")
        if route and route != sent_route:
            sent_route = route
            seq += 1
            await _send_frame(
                websocket,
                seq,
                FRAME_SKELETON,
                {
                    "phase": "routing",
                    "route": route,
                    "text": SKELETONS.get(route, SKELETONS["routing"]),
                },
            )

    # ── ③ 回答帧 ────────────────────────────────────────────────────────────────
    answer_ms = (time.perf_counter() - started) * 1000
    response = build_response(turn, actor=actor, settings=settings)
    seq += 1
    await _send_frame(
        websocket,
        seq,
        FRAME_ANSWER,
        {
            "text": response.answer,
            "done": True,  # 目前是整段产出（见模块 docstring 的「诚实的边界」）
            "degraded": bool(turn.exhausted),
            "citations": response.citations,
            "chart": bool(response.chart_spec),
        },
    )

    # ── ④ 收尾帧 ────────────────────────────────────────────────────────────────
    seq += 1
    await _send_frame(
        websocket,
        seq,
        FRAME_DONE,
        {
            "flow_state": response.flow_state,
            "thread_id": response.thread_id,
            "session_id": response.session_id,
            "route": response.route,
            "blocked": response.blocked,
            "approval": response.approval.model_dump(mode="json") if response.approval else None,
            "latency_ms": round(answer_ms, 2),
            "first_frame_ms": round(first_frame_ms, 2),
            "debug": response.debug.model_dump(mode="json") if response.debug else None,
        },
    )

    if metrics is not None:
        endpoint = {"endpoint": "WS /api/chat/stream"}
        metrics.counter(
            "shopsage_api_chat_turns_total", {**endpoint, "flow_state": response.flow_state}
        )
        metrics.observe("shopsage_api_chat_turn_seconds", answer_ms / 1000, endpoint)
        metrics.observe("shopsage_api_stream_first_frame_seconds", first_frame_ms / 1000, endpoint)
        metrics.observe("shopsage_api_stream_answer_seconds", answer_ms / 1000, endpoint)
    logger.info(
        "WS 完成：thread=%s flow=%s 首帧=%.1fms 回答=%.1fms 路径=%s",
        actor.thread_id,
        response.flow_state,
        first_frame_ms,
        answer_ms,
        "→".join(turn.visited),
    )
    return seq


async def _send_frame(websocket: WebSocket, seq: int, event: str, data: dict[str, Any]) -> None:
    """发一帧（形状统一：`event` / `seq` / `ts` / `data`）。

    ⚠️ 发送失败（客户端已断开）**不抛**：`run_turn` 里的图还在跑（状态要落 checkpoint），
    让「发不出去」把整轮打断，等于用客户端的网络质量决定服务端要不要保存状态。
    """
    try:
        await websocket.send_json({"event": event, "seq": seq, "ts": time.time(), "data": data})
    except Exception as exc:  # noqa: BLE001 - 连接已断：记录但继续
        logger.debug("WS 帧发送失败（连接可能已断开）：%s", exc)


__all__ = [
    "FRAME_SAFE_KEYS",
    "NODE_PROGRESS",
    "SKELETONS",
    "approval_gate",
    "build_response",
    "chat",
    "chat_stream",
    "entry_state",
    "router",
    "safe_delta",
    "shape_debug",
]
