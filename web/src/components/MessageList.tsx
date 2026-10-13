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
import { parseCitations } from "@/lib/contracts";

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
  /** 图表的文字摘要（图表本体在 `chartSpec`，由页面渲染） */
  note?: string;
}

export function flowBadge(state: Message["flowState"], blocked?: boolean) {
  if (state === "approval_required") return { text: "待人工确认", cls: "warn" };
  if (state === "blocked" || blocked) return { text: "已被拦下", cls: "danger" };
  if (state === "delivered") return { text: "已答完", cls: "ok" };
  return { text: "", cls: "" };
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
