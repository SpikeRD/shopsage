"use client";

/**
 * 客户订单查询页（`/orders`）—— 变更 003 / 需求 2。
 *
 * ## 这一页解决什么
 *
 * 变更 003 之前客户**没有任何订单入口**：`/orders` 不存在，`app/db/queries.py` 里
 * 现成的对客查询（带行级过滤）没有出口。客户想退款只能靠对话里的一句
 * 「帮我退款」，而他还看不到「哪一单能退、还剩多少」。
 *
 * 现在这一页给三样东西：
 *   ① **订单列表**（订单号 / 状态 / 金额 / 时间），点开看明细；
 *   ② **该单的退款记录**（谁在什么时候申请了什么、批了没、审批意见）；
 *   ③ **直接申请退款**（金额默认填剩余可退额度、可改小；备注可选）。
 *
 * ## 四条纪律
 *
 * ### ① 不做任何业务判断
 *
 * 「这单能不能退」由后端的 `refundable` 字段回答（后端用
 * `REFUNDABLE_ORDER_STATUSES` + 剩余额度算出来）。前端**不重算**这条规则 ——
 * 重算一份就多一处会漂移的真相，而漂移的症状是「按钮亮着但提交必失败」。
 *
 * ### ② 金额原样当字符串
 *
 * 后端给的是十进制字符串（`"58.00"`）。前端做算术要转 `Number`，但**发回去时用字符串** ——
 * 避免 `0.1+0.2` 那类二进制尾巴被送回后端参与额度比较。
 *
 * ### ③ 脱敏字段照实展示
 *
 * `payment_method` 对客户是 `***`（财务级字段）。这是**有值但看不到**，
 * 与 `null`（真的没有支付方式）语义不同，所以两者分别显示成 `***` 与 `—`。
 *
 * ### ④ 幂等键在**打开表单时**生成一次
 *
 * 同一个键重复提交只落一单（后端有复合唯一约束）。关掉再打开会得到新键 ——
 * 那是一次新的申请意图，正确。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { getOrders, postRefundRequest } from "@/lib/api";
import { browserStorage, canRequestRefund, type Entry } from "@/lib/auth";
import { withFreshToken } from "@/lib/session";
import { type Order, orderStatusLabel, refundStatusLabel, type Refund } from "@/lib/contracts";
import { apiBase } from "@/lib/config";
import {
  describeApiFailure as describe,
  formatTime,
  lineTotal,
  MAX_NOTE_CHARS,
  newRefundKey,
  refundBadgeClass,
} from "@/lib/refund_ui";
import { useSession } from "@/components/useChatStream";

const ENTRY: Entry = "customer";

export default function OrdersPage() {
  const { session, ready } = useSession(ENTRY);

  // ⚠️ 与对客页/对内页同一条口径：**不做强制 redirect**。
  // 演示环境里「一个能读的说明页 + 一个登录入口」比「莫名其妙跳走」好排查得多。
  if (!ready) return <div className="card muted">正在检查登录状态…</div>;
  if (!session) {
    return (
      <div className="card">
        <h1 className="card-title">我的订单：需要先登录</h1>
        <p className="muted">
          这一页调用的是受保护接口，没有令牌时后端会回 401。去{" "}
          <Link href="/login">登录</Link> 即可。
        </p>
      </div>
    );
  }
  return <OrderConsole token={session.token} role={session.role} subject={session.userId} />;
}

function OrderConsole({ token, role, subject }: { token: string; role: string; subject: string }) {
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  /** 正在填退款申请的那一单（`null` = 没有表单打开） */
  const [form, setForm] = useState<{
    order: Order;
    amount: string;
    reason: string;
    /** ⚠️ 打开表单时生成一次，提交过程中不变 —— 重复点只落一单 */
    idempotencyKey: string;
  } | null>(null);

  const allowed = canRequestRefund(role);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // ⚠️ 走 `withFreshToken`（issue 003 的 P1-2）：令牌快过期时先续期、
      //    被 401 拒时自动换一条重试一次，而不是把用户踢回登录页。
      const payload = await withFreshToken(ENTRY, browserStorage(), (fresh) => getOrders(fresh));
      setOrders(payload.orders);
    } catch (exc) {
      setNotice({ kind: "error", text: describe(exc) });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openForm = (order: Order) => {
    setNotice(null);
    setForm({
      order,
      // 默认填**剩余可退额度**（后端算好的），客户可以改小
      amount: order.remaining_refundable,
      reason: "",
      idempotencyKey: newRefundKey(),
    });
  };

  const submit = async () => {
    if (!form) return;
    const amount = form.amount.trim();
    if (!/^\d+(\.\d{1,2})?$/.test(amount) || Number(amount) <= 0) {
      setNotice({ kind: "error", text: "退款金额要写成大于 0 的数字，最多两位小数（例如 58.00）。" });
      return;
    }
    setNotice(null);
    try {
      const refund: Refund = await withFreshToken(ENTRY, browserStorage(), (fresh) =>
        postRefundRequest(fresh, {
          orderId: form.order.order_id,
          amount,
          reason: form.reason.trim() || null,
          idempotencyKey: form.idempotencyKey,
        }),
      );
      setForm(null);
      await load();
      setNotice({
        kind: "ok",
        text: `已提交退款申请：单号 ${refund.refund_id}，金额 ${refund.amount} 元（状态：${refundStatusLabel(refund.status)}，等待平台审核）。`,
      });
    } catch (exc) {
      // ⚠️ 失败时**保留表单**：客户改一个金额就能重试，不必重填一整遍
      //    （与 issue 004 在管理台修的那条口径一致：失败清空表单是坏的体验）。
      setNotice({ kind: "error", text: describe(exc) });
    }
  };

  const refundCount = useMemo(
    () => orders.reduce((total, order) => total + order.refunds.length, 0),
    [orders],
  );

  return (
    <div className="stack">
      <div className="card">
        <div className="row between">
          <div>
            <h1 className="card-title" style={{ marginBottom: 2 }}>
              我的订单
            </h1>
            <div className="small muted">
              后端 <span className="mono">{apiBase()}</span> · 身份{" "}
              <span className="chip">
                {subject} · {role}
              </span>{" "}
              · 共 {orders.length} 笔订单、{refundCount} 条退款记录
            </div>
          </div>
          <div className="row">
            {!allowed ? <span className="badge warn">当前身份不能申请退款</span> : null}
            <button className="btn ghost tiny" type="button" disabled={loading} onClick={() => void load()}>
              {loading ? "刷新中…" : "刷新"}
            </button>
            <Link className="btn ghost tiny" href="/">
              去对客问答
            </Link>
          </div>
        </div>
        <ul className="hint-list">
          <li>
            这里**只能看到你自己的订单**（行级过滤在后端：查询固定带{" "}
            <span className="mono">WHERE user_id = 本人</span>）。
          </li>
          <li>
            退款申请提交后状态是「待审批」——要等平台客服处理，客户本人**不能**审批自己的退款。
          </li>
          <li>
            金额默认填「剩余可退额度」，可以改小；超过额度的会被后端拒绝并说明原因。
          </li>
        </ul>
      </div>

      {notice ? (
        <div className={notice.kind === "ok" ? "card" : "error-box"}>
          {notice.kind === "ok" ? <span className="badge ok">成功</span> : null} {notice.text}
        </div>
      ) : null}

      {form ? (
        <div className="card">
          <div className="row between">
            <h2 className="card-title" style={{ marginBottom: 0 }}>
              申请退款 · 订单 <span className="mono">{form.order.order_id}</span>
            </h2>
            <button className="btn ghost tiny" type="button" onClick={() => setForm(null)}>
              取消
            </button>
          </div>
          <p className="muted small">
            该订单金额 {form.order.total_amount} 元，剩余可退额度{" "}
            <strong>{form.order.remaining_refundable}</strong> 元。
          </p>
          <div className="row" style={{ marginTop: 8 }}>
            <input
              className="input"
              style={{ maxWidth: 160 }}
              aria-label="退款金额"
              placeholder="退款金额"
              value={form.amount}
              onChange={(event) => setForm({ ...form, amount: event.target.value })}
            />
            <input
              className="input"
              style={{ maxWidth: 420 }}
              aria-label="退款备注"
              placeholder="备注 / 申请理由（可选，最长 255 字）"
              maxLength={MAX_NOTE_CHARS}
              value={form.reason}
              onChange={(event) => setForm({ ...form, reason: event.target.value })}
            />
            <button className="btn" type="button" onClick={() => void submit()}>
              提交申请
            </button>
          </div>
          <p className="small muted" style={{ marginTop: 6 }}>
            幂等键 <span className="mono">{form.idempotencyKey}</span>（打开表单时生成：
            重复点提交只会落一单）。
          </p>
        </div>
      ) : null}

      {orders.length === 0 ? (
        <div className="card muted">{loading ? "加载中…" : "你还没有订单。"}</div>
      ) : null}

      {orders.map((order) => (
        <div className="card" key={order.order_id}>
          <div className="row between">
            <div>
              <h2 className="card-title" style={{ marginBottom: 2 }}>
                订单 <span className="mono">{order.order_id}</span>
              </h2>
              <div className="small muted">
                <span className="chip">{orderStatusLabel(order.status)}</span>{" "}
                <span className="chip">金额 {order.total_amount} 元</span>{" "}
                <span className="chip">
                  支付方式 {order.payment_method === null ? "—" : order.payment_method}
                </span>{" "}
                <span className="chip">下单 {formatTime(order.created_at)}</span>
              </div>
            </div>
            <div className="row">
              <span className="badge">可退 {order.remaining_refundable} 元</span>
              <button
                className="btn ghost tiny"
                type="button"
                onClick={() => setExpanded(expanded === order.order_id ? null : order.order_id)}
              >
                {expanded === order.order_id ? "收起明细" : "查看明细"}
              </button>
              <button
                className="btn tiny"
                type="button"
                disabled={!allowed || !order.refundable || form !== null}
                title={
                  !allowed
                    ? "当前身份没有申请退款的权限"
                    : order.refundable
                      ? "为这笔订单提交退款申请"
                      : "这笔订单此刻不能申请退款（状态不支持，或剩余可退额度为 0）"
                }
                onClick={() => openForm(order)}
              >
                申请退款
              </button>
            </div>
          </div>

          {expanded === order.order_id ? (
            <div style={{ marginTop: 10 }}>
              <div className="scroll-x">
                <table className="data">
                  <thead>
                    <tr>
                      <th>商品</th>
                      <th>数量</th>
                      <th>单价</th>
                      <th>小计</th>
                    </tr>
                  </thead>
                  <tbody>
                    {order.items.map((item) => (
                      <tr key={item.order_item_id}>
                        <td className="cell-ellipsis" title={item.product_name ?? `商品 #${item.product_id}`}>
                          {item.product_name ?? `商品 #${item.product_id}`}
                        </td>
                        <td>{item.quantity}</td>
                        <td className="mono">{item.unit_price}</td>
                        <td className="mono">{lineTotal(item.quantity, item.unit_price)}</td>
                      </tr>
                    ))}
                    {order.items.length === 0 ? (
                      <tr>
                        <td colSpan={4} className="muted small">
                          没有明细行。
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          <div style={{ marginTop: 10 }}>
            <div className="small muted">
              退款记录（{order.refunds.length} 条）· 已占用可退额度 {order.consumed_amount} 元
            </div>
            {order.refunds.length === 0 ? (
              <div className="small muted">这笔订单还没有退款记录。</div>
            ) : (
              <div className="scroll-x">
                <table className="data">
                  <thead>
                    <tr>
                      <th>退款单号</th>
                      <th>金额</th>
                      <th>状态</th>
                      <th>申请理由</th>
                      <th>审批意见</th>
                      <th>申请时间</th>
                      <th>审批时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {order.refunds.map((item) => (
                      <tr key={item.refund_id}>
                        <td className="mono">{item.refund_id}</td>
                        <td className="mono">{item.amount}</td>
                        <td>
                          <span className={`badge ${refundBadgeClass(item.status)}`}>
                            {refundStatusLabel(item.status)}
                          </span>
                        </td>
                        <td className="cell-ellipsis" title={item.reason ?? ""}>
                          {item.reason ?? "—"}
                        </td>
                        <td className="cell-ellipsis" title={item.decision_note ?? ""}>
                          {item.decision_note ?? "—"}
                        </td>
                        <td className="small muted">{formatTime(item.created_at)}</td>
                        <td className="small muted">{formatTime(item.decided_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
