"""变更 003 的端到端取证：**入口贯通 + 退款申请/审批闭环 + 客户不能自审批**。

一次性取证脚本（不是测试套件）—— 它走**真的 HTTP + 真的库**，
而用例里那些断言是「组件级」的。两者的价值不同：

| | 用例（`uv run pytest`） | 本脚本 |
|---|---|---|
| 覆盖 | 每个组件的边界 | **三条需求串起来能不能跑通** |
| 依赖 | 可离线（假模型/假库） | 需要后端在跑 + 演示数据已灌 |
| 输出 | 断言通过/失败 | **原始 JSON 片段**（可直接贴进变更单） |

⚠️ 为什么要写成文件而不是 PowerShell 一行命令：**中文请求体经 PowerShell 传递会变乱码**
（变更 002 实测：`我的订单到哪了？` 到后端变成 `???????`）。这不是后端问题，
是 shell 编码问题 —— 所以取证脚本必须自己控制 UTF-8 编码。

⚠️ 本脚本会**真的改演示库**：它提交退款申请、审批它们，并按 `evidence3-` 前缀的
幂等键收尾清理。它只该对着**开发/演示库**跑。

⚠️ 后端地址可用 `SHOPSAGE_API` 覆盖（默认 `http://127.0.0.1:8000`）。
这条不是可有可无的：改代码之后**原来那个进程还在跑旧代码**（实测：
`/api/orders` 返回 404 而 `/api/chat/approve` 还活着 —— 那是变更 003 之前的路由表）。
这时正确做法是**另起一个实例在别的端口**核对新代码，而不是把别人的进程杀掉。

用法（先确认后端在跑、演示数据已灌）：

    uv run python -m scripts.evidence_refunds
    $env:SHOPSAGE_API="http://127.0.0.1:8001"; uv run python -m scripts.evidence_refunds
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

#: 后端地址。默认 8000（项目惯例），可用 `SHOPSAGE_API` 覆盖 —— 见模块 docstring 的说明。
API = os.getenv("SHOPSAGE_API", "http://127.0.0.1:8000").rstrip("/")

#: 本脚本造的退款单用的幂等键前缀（收尾时按它清理）
KEY_PREFIX = "evidence3-"


def login(identifier: str, password: str) -> str:
    """账号 + 口令换令牌（**演示口令刻意不打印**）。"""
    payload = json.dumps({"identifier": identifier, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        f"{API}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))["access_token"]


def call(
    path: str, token: str, body: dict[str, object] | None = None, *, method: str | None = None
) -> tuple[int, dict[str, object]]:
    """发一个请求，返回 `(状态码, 响应体)`；4xx/5xx **不抛**（它们的响应体就是要看的东西）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{API}{path}", data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else {}


def error_code(payload: dict[str, object]) -> str:
    """从错误信封里取 `code`（排障时它是稳定标识，文案不是）。"""
    error = payload.get("error")
    return str(error.get("code")) if isinstance(error, dict) else ""


def cleanup() -> None:
    """清掉本脚本造的退款行（按幂等键前缀）。**必须在结束时跑**，否则会永久吃掉可退额度。"""
    from sqlalchemy import delete

    from app.db.models import Refund
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        removed = db.execute(
            delete(Refund).where(Refund.idempotency_key.like(f"{KEY_PREFIX}%"))
        ).rowcount
        db.commit()
    if removed:
        print(f"（收尾）已清理本脚本造的退款行：{removed} 条")


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}：{detail}")

    # 口令取自 scripts/seed.py 的 DEMO_ACCOUNTS（刻意不打印）
    customer = login("13800000002", "customer-demo-pass")
    service = login("service", "service-demo-pass")  # 变更 003 新增的客服演示账号
    admin = login("admin", "admin-demo-pass")

    try:
        # ── 需求 1：入口贯通（同一枚令牌驱动多个入口）────────────────────────
        print("=== 需求 1：管理员入口贯通 ===")
        status, me = call("/api/auth/me", admin)
        check("管理员 /me", status == 200 and me.get("entry") == "admin", f"HTTP {status} entry={me.get('entry')}")
        perms = me.get("permissions") if isinstance(me.get("permissions"), list) else []
        check(
            "管理员持有全部权限点",
            all(p in perms for p in ("read:own", "read:all_orders", "read:finance", "write:refund", "write:ticket", "request:refund", "approve:refund", "admin")),
            f"{len(perms)} 个：{' '.join(sorted(str(p) for p in perms))}",
        )
        status, accounts = call("/api/admin/accounts", admin)
        check("管理台可用（同一令牌）", status == 200, f"HTTP {status}")
        status, refunds = call("/api/refunds", admin)
        check("对内退款审批台可用（同一令牌）", status == 200, f"HTTP {status}")
        status, orders = call("/api/orders", admin)
        check(
            "对客订单口：授权成立但无客户身份 → 400（不是 401/404）",
            status == 400 and error_code(orders) == "no_customer_identity",
            f"HTTP {status} code={error_code(orders)}",
        )

        # ── 需求 2：客户订单页 + 申请退款 + **不能自审批**─────────────────────
        print()
        print("=== 需求 2：客户订单页与申请退款 ===")
        status, orders = call("/api/orders", customer)
        rows = orders.get("orders") if isinstance(orders.get("orders"), list) else []
        check("客户能查自己的订单", status == 200 and bool(rows), f"HTTP {status} 订单数={len(rows)}")
        refundable = [row for row in rows if isinstance(row, dict) and row.get("refundable")]
        if not refundable:
            print("  SKIP  没有可退订单：先跑 `uv run python -m scripts.seed`")
            return 1
        target = refundable[0]
        order_id = int(target["order_id"])
        remaining = str(target["remaining_refundable"])
        payment = target.get("payment_method")
        print(f"  样本订单 {order_id}：金额 {target.get('total_amount')} 剩余可退 {remaining} 支付方式 {payment!r}")
        check(
            "金额明文可见（决策 D1：amount 不在脱敏规则表里）",
            target.get("total_amount") not in (None, "", "***"),
            f"total_amount={target.get('total_amount')!r}",
        )
        check(
            "支付方式是 *** 或 null（财务级字段，客户看不到明文）",
            payment in ("***", None),
            f"payment_method={payment!r}",
        )

        key = f"{KEY_PREFIX}manual-{order_id}"
        status, created = call(
            "/api/refunds",
            customer,
            {"order_id": order_id, "amount": "1.00", "reason": "变更 003 取证：手动申请", "idempotency_key": key},
        )
        refund_id = int(created.get("refund_id") or 0)
        check(
            "客户提交退款申请 → 201 且状态是 requested",
            status == 201 and created.get("status") == "requested",
            f"HTTP {status} refund_id={refund_id} status={created.get('status')}",
        )

        # ★这是需求 2 的核心判据：客户**批不了**自己的退款
        status, attempted = call(
            "/api/refunds/decision", customer, {"refund_id": refund_id, "decision": "approve"}
        )
        check(
            "★客户审批退款被拒（404，越权对外口径）",
            status == 404 and error_code(attempted) == "not_found_or_forbidden",
            f"HTTP {status} code={error_code(attempted)}",
        )
        status, legacy = call("/api/chat/approve", customer, {"thread_id": "c2:x", "approved": True})
        check("★旧的对话轮审批接口已删除（404）", status == 404, f"HTTP {status}")

        # 幂等：同一个键重复提交只落一单
        status, again = call(
            "/api/refunds",
            customer,
            {"order_id": order_id, "amount": "1.00", "reason": "变更 003 取证：手动申请", "idempotency_key": key},
        )
        check(
            "重复提交同一个幂等键 → 同一张单",
            status == 201 and int(again.get("refund_id") or 0) == refund_id,
            f"HTTP {status} refund_id={again.get('refund_id')}",
        )

        # ── 需求 3：员工查询 + 审批 + 状态机 ─────────────────────────────────
        print()
        print("=== 需求 3：员工退款审批台 ===")
        status, me_service = call("/api/auth/me", service)
        check(
            "客服的入口是对内（否则看不到审批台）",
            status == 200 and me_service.get("entry") == "internal",
            f"HTTP {status} entry={me_service.get('entry')}",
        )
        status, listed = call(f"/api/refunds?refund_id={refund_id}", service)
        found = [row for row in (listed.get("refunds") or []) if isinstance(row, dict)]
        check("员工能按单号查到那张退款单", status == 200 and bool(found), f"HTTP {status} 命中={len(found)}")

        status, decided = call(
            "/api/refunds/decision",
            service,
            {"refund_id": refund_id, "decision": "approve", "note": "变更 003 取证：已核实"},
        )
        check(
            "员工审批通过 → approved 且四样留痕都落库",
            status == 200
            and decided.get("status") == "approved"
            and decided.get("decided_by") is not None
            and decided.get("decided_at") is not None
            and decided.get("approver_role") == "customer_service"
            and decided.get("decision_note") == "变更 003 取证：已核实",
            f"HTTP {status} status={decided.get('status')} decided_by={decided.get('decided_by')} "
            f"role={decided.get('approver_role')}",
        )
        status, conflict = call(
            "/api/refunds/decision", service, {"refund_id": refund_id, "decision": "reject"}
        )
        detail = conflict.get("error")
        current = detail.get("detail") if isinstance(detail, dict) else {}
        check(
            "重复审批 → 409 且带上当前状态",
            status == 409 and error_code(conflict) == "refund_not_decidable" and isinstance(current, dict) and current.get("current_status") == "approved",
            f"HTTP {status} code={error_code(conflict)} current_status={current.get('current_status') if isinstance(current, dict) else None}",
        )

        # ── LLM 路径：一句话真的落库（或如实说明没有可退订单）─────────────
        print()
        print("=== 需求 2（LLM 路径）：一句话申请退款 ===")
        status, turn = call(
            "/api/chat",
            customer,
            {"message": "帮我申请一笔退款", "session_id": "evidence3-llm"},
        )
        refund_outcome = turn.get("refund") if isinstance(turn.get("refund"), dict) else None
        answer = str(turn.get("answer") or "")
        route = turn.get("route")
        print(f"  HTTP {status} route={route} flow_state={turn.get('flow_state')}")
        print(f"  answer: {answer[:160]}")
        print(f"  refund: {json.dumps(refund_outcome, ensure_ascii=False) if refund_outcome else '（无）'}")
        # 两种都算通过，但必须是**诚实的一种**：
        #   * submitted → 真落库了；
        #   * needs_order_id / refused → 如实说明（没有编造「已提交」）
        check(
            "LLM 退款路径给了结构化结果（submitted 或如实说明）",
            refund_outcome is not None
            and refund_outcome.get("status") in ("submitted", "needs_order_id", "refused"),
            f"status={refund_outcome.get('status') if refund_outcome else None}",
        )
        check(
            "★对客路径不再挂起等批准（没有 approval）",
            turn.get("approval") is None and turn.get("flow_state") != "approval_required",
            f"flow_state={turn.get('flow_state')} approval={turn.get('approval')}",
        )
        check("LLM 路由判成 refund（不是 ticket）", route == "refund", f"route={route}")

        print()
        print("=== 汇总 ===")
        for name, ok, detail in results:
            print(f"{'PASS' if ok else 'FAIL'}  {name:<44} {detail}")
        failed = [name for name, ok, _ in results if not ok]
        print()
        print(f"共 {len(results)} 条：{len(results) - len(failed)} 通过、{len(failed)} 失败")
        return 1 if failed else 0
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
