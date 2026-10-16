"use client";

/**
 * 消息列表 + 出处展示（**纯展示组件**：不发起任何请求、不做任何判断）。
 *
 * 拆出来的理由：这一块是「溯源可视化」的落点（`16.web.md` §4 第 3 条），
 * 而它最容易变成一坨难测的东西。它现在的输入只有 props，
 * 所以「出处串怎么解析、空值怎么显示」这些逻辑全在 `lib/contracts.ts` 里，
 * 由 `web/tests/citations.test.ts` 直接测。
 */

import Link from "next/link";
import { parseCitations, refundStatusLabel, type RefundOutcome } from "@/lib/contracts";

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  /** assistant 消息才有 */
  citations?: string[];
  flowState?: "delivered" | "blocked" | "approval_required" | null;
  route?: string;
  blocked?: boolean;
  refusals?: string[];
  degraded?: boolean;
  /** 变更 003：退款申请的结果（有值时才渲染那张卡片） */
  refund?: RefundOutcome | null;
  /** 图表的文字摘要（图表本体在 `chartSpec`，由页面渲染） */
  note?: string;
}

export function flowBadge(state: Message["flowState"], blocked?: boolean) {
  // ⚠️ 变更 003：`approval_required` 这一支**保留但不指向按钮**。
  // 对客闸门整体移除后它在生产路径上不可达（决策 D2：契约不收缩），
  // 万一将来真的出现，文案也必须说清「去哪儿处理」，而不是让用户找一个不存在的按钮。
  if (state === "approval_required") return { text: "已转人工", cls: "warn" };
  if (state === "blocked" || blocked) return { text: "已被拦下", cls: "danger" };
  if (state === "delivered") return { text: "已答完", cls: "ok" };
  return { text: "", cls: "" };
}

/**
 * 退款结果卡片（变更 003）。
 *
 * ⚠️ 三种形状必须**分开**渲染（`submitted` / `needs_order_id` / `refused`）：
 * 把 `needs_order_id` 也画成「已提交」会让客户以为申请成功了，
 * 而系统其实还在等他确认是哪一笔订单 —— 那是资金类界面最不该有的误导。
 */
export function RefundCard({ outcome }: { outcome: RefundOutcome }) {
  if (outcome.status === "submitted") {
    return (
      <div className="refund-card ok">
        <div className="row between">
          <strong>{outcome.idempotent_hit ? "退款申请已提交（此前提交过同一笔）" : "退款申请已提交"}</strong>
          <span className="badge warn">等待平台审核</span>
        </div>
        <ul className="hint-list" style={{ marginBottom: 0 }}>
          <li>
            退款单号 <span className="mono">{outcome.refund_id}</span> · 订单号{" "}
            <span className="mono">{outcome.order_id}</span>
          </li>
          <li>退款金额 {outcome.amount} 元</li>
          {outcome.remaining_after ? <li>该订单剩余可退额度 {outcome.remaining_after} 元</li> : null}
          <li>钱**还没有**退到账上：这笔申请已提交，等平台审核通过后才处理。</li>
        </ul>
      </div>
    );
  }
  if (outcome.status === "needs_order_id") {
    return (
      <div className="refund-card warn">
        <strong>需要你确认是哪一笔订单</strong>
        <p style={{ margin: "6px 0" }}>
          你有 {outcome.candidates?.length ?? 0} 笔可退订单，
          <strong>目前还没有提交任何退款</strong>。请告诉我订单号：
        </p>
        <div className="row">
          {(outcome.candidates ?? []).map((id) => (
            <span className="chip mono" key={id}>
              {id}
            </span>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className="refund-card danger">
      <strong>退款申请没有提交成功</strong>
      <p style={{ margin: "6px 0" }}>{outcome.hint || "请稍后重试，或到「我的订单」里手动申请。"}</p>
      <Link className="btn ghost tiny" href="/orders">
        去「我的订单」手动申请
      </Link>
    </div>
  );
}

/** 出处列表：`来源文档` + 页码/片段 chip；`href` 指向可定位的出处页（§4.3）。 */
export function Citations({ citations }: { citations: string[] }) {
  if (!citations || citations.length === 0) return null;
  const parsed = parseCitations(citations);
  return (
    <details className="citations">
      <summary>来源 {parsed.length} 条（点开看文档 / 页码 / 片段）</summary>
      <div className="stack" style={{ marginTop: 6 }}>
        {parsed.map((item) => (
          <div className="citation-item" key={item.raw}>
            <Link href={`/citation?q=${encodeURIComponent(item.raw)}`}>{item.document}</Link>
            {item.page !== null ? <span className="chip">p.{item.page}</span> : null}
            {item.chunkId ? <span className="chip">#{item.chunkId}</span> : null}
          </div>
        ))}
      </div>
    </details>
  );
}

export function MessageBubble({ message }: { message: Message }) {
  const badge = flowBadge(message.flowState, message.blocked);
  return (
    <div className={`bubble ${message.role}`}>
      <div className="bubble-head">
        <strong>{message.role === "user" ? "我" : message.role === "system" ? "系统" : "ShopSage"}</strong>
        {badge.text ? <span className={`badge ${badge.cls}`}>{badge.text}</span> : null}
        {message.route ? <span className="badge accent">路径 {message.route}</span> : null}
        {message.degraded ? <span className="badge warn">降级（重放预算用尽）</span> : null}
      </div>
      <div className="bubble-body">{message.text}</div>
      {message.note ? <div className="small muted" style={{ marginTop: 6 }}>{message.note}</div> : null}
      {/* 变更 003：退款结果卡片（三种形状分别渲染，见 RefundCard 的注释） */}
      {message.refund ? <RefundCard outcome={message.refund} /> : null}
      {message.refusals && message.refusals.length > 0 ? (
        <ul className="hint-list">
          {message.refusals.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : null}
      {message.citations ? <Citations citations={message.citations} /> : null}
    </div>
  );
}

export function MessageList({ messages }: { messages: Message[] }) {
  return (
    <div className="messages" data-testid="messages">
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}
    </div>
  );
}
