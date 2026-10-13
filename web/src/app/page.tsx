"use client";

/**
 * 对客入口（`/`）：聊天框 + 流式渲染 + 溯源 + 人机审批。
 *
 * 这一页只做四件事（`16.web.md` §4）：
 * ① 分流 —— 没登录就去登录；② 流式展示；③ 展示出处；④ 展示「被拦下 / 待确认」。
 * **没有任何业务判断**：能不能查到这行数据、这句话能不能答，全在后端。
 */

import { useMemo, useState, useEffect } from "react";
import Link from "next/link";
import { apiBase, configWarnings, resolvedApiBase, streamUrl } from "@/lib/config";
import { canUseInternal } from "@/lib/auth";
import { Composer } from "@/components/Composer";
import { MessageList } from "@/components/MessageList";
import { StreamStatus } from "@/components/StreamStatus";
import { useChatStream, useSession } from "@/components/useChatStream";

const SUGGESTIONS = [
  "我的订单到哪了？",
  "退货政策是怎么规定的？",
  "帮我申请一笔退款",
  "我的积分有多少？",
];

export default function CustomerChatPage() {
  const { session, ready } = useSession("customer");

  // ⚠️ 登录态的读取必须在**客户端 effect** 里发生（`localStorage` 只有浏览器有），
  // 这件事已经由 `useSession` 负责（它返回 `ready` 之前不渲染结论）。
  // 本页刻意**不做强制 redirect**：演示环境里一个能读的说明页 + 一个登录入口，
  // 比「莫名其妙跳走」好排查得多（runbook §10.2 第 3 条）。
  if (!ready) {
    return <div className="card muted">正在检查登录状态…</div>;
  }
  if (!session) {
    return (
      <div className="card">
        <h1 className="card-title">对客入口：需要先登录</h1>
        <p className="muted">
          这一页调用的是受保护接口（<span className="mono">Authorization: Bearer &lt;JWT&gt;</span>），
          没有令牌时后端会回 401。演示环境用一个本机签发的令牌登录即可 —— 见{" "}
          <Link href="/login">登录 / 切角色</Link>。
        </p>
      </div>
    );
  }
  return <ChatWorkspace token={session.token} role={session.role} userId={session.userId} entry="customer" />;
}

/**
 * 聊天工作区（登录后才有）。
 *
 * ⚠️ **不导出**：`next build` 对 `app/**\/page.tsx` 做入口检查，任何额外导出都会
 * 让构建失败（实测：`Property 'ChatWorkspace' is incompatible with index signature`）。
 * 要复用的部分一律放 `src/components/` 或 `src/lib/`。
 */
function ChatWorkspace({
  token,
  role,
  userId,
  entry,
}: {
  token: string;
  role: string;
  userId: string;
  entry: "customer" | "internal";
}) {
  const chat = useChatStream({ entry, token });
  const refused = useMemo(() => chat.messages.flatMap((message) => message.refusals ?? []), [chat.messages]);
  const [reason, setReason] = useState("");
  // ⚠️ 配置问题要在**发请求之前**就说（坑 14）：`NEXT_PUBLIC_API_BASE` 指到前端自己时，
  // HTTP 可能看着正常（同源的 /api/kb 由 Next 自己答）而 WS 一定失败 —— 那种「半通」最难查
  const [configNotes, setConfigNotes] = useState<string[]>([]);
  useEffect(() => {
    setConfigNotes(configWarnings());
  }, []);

  return (
    <div className="stack">
      <div className="card">
        <div className="row between">
          <div>
            <h1 className="card-title" style={{ marginBottom: 2 }}>
              对客问答（流式）
            </h1>
            <div className="small muted">
              后端 <span className="mono">{apiBase()}</span>
              {apiBase() !== resolvedApiBase() ? (
                <>
                  {" "}
                  → 实际用 <span className="mono">{resolvedApiBase()}</span>
                </>
              ) : null}{" "}
              · 流式 <span className="mono">{streamUrl()}</span> · 身份{" "}
              <span className="chip">
                user_id={userId} · {role}
              </span>{" "}
              · 会话 <span className="chip">{chat.sessionId || "（生成中）"}</span>
            </div>
          </div>
          <div className="row">
            {canUseInternal(role) ? (
              <Link className="btn ghost tiny" href="/internal">
                切到对内查询
              </Link>
            ) : null}
            <button className="btn ghost tiny" type="button" onClick={chat.reset}>
              新会话
            </button>
          </div>
        </div>
      </div>

      {chat.messages.length === 0 ? (
        <div className="card">
          <p className="muted" style={{ marginTop: 0 }}>
            试试这些（点一下直接问）：
          </p>
          <div className="row">
            {SUGGESTIONS.map((text) => (
              <button
                className="btn ghost tiny"
                key={text}
                type="button"
                disabled={chat.running}
                onClick={() => chat.send(text)}
              >
                {text}
              </button>
            ))}
          </div>
          <ul className="hint-list">
            <li>首帧（骨架）来自后端：先出「正在理解你的问题…」，再补节点与回答。</li>
            <li>回答底部的「来源」可点开看文档 / 页码 / 片段。</li>
            <li>敏感操作会挂起（<span className="mono">approval_required</span>），需要在这里点「批准」或「拒绝」。</li>
          </ul>
        </div>
      ) : null}

      {configNotes.length > 0 ? (
        <div className="error-box">
          <strong>配置问题（发请求之前先看这条）</strong>
          <ul className="hint-list">
            {configNotes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="card">
        <MessageList messages={chat.messages} />
        <StreamStatus state={chat.stream} />
        {chat.error ? (
          <div className="error-box" style={{ marginTop: 8 }}>
            {chat.error}
          </div>
        ) : null}
        {refused.length > 0 ? (
          <ul className="hint-list">
            {refused.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : null}
        <div style={{ marginTop: 12 }}>
          <Composer running={chat.running} onSubmit={chat.send} />
        </div>
      </div>

      {chat.stream.approval ? (
        <div className="approval">
          <strong>需要人工确认</strong>
          <p style={{ margin: "6px 0" }}>{chat.stream.approval.question}</p>
          <div className="small muted mono">thread_id = {chat.stream.approval.threadId}</div>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn" type="button" onClick={() => void chat.approve(true, reason)}>
              批准并继续
            </button>
            <button className="btn ghost" type="button" onClick={() => void chat.approve(false, reason)}>
              拒绝（按未执行处理）
            </button>
            <input
              className="input"
              style={{ maxWidth: 320 }}
              placeholder="审批意见（可选，进审计日志）"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>
        </div>
      ) : null}

      {chat.stream.debug ? (
        <div className="card">
          <strong className="small">排障载荷（内部角色 + API_DEBUG_PAYLOAD 打开时才有）</strong>
          <div className="small muted">
            走过节点：{chat.stream.debug.visited.join(" → ") || "（无）"}
            {chat.stream.debug.rowCount !== null ? ` · 行数 ${chat.stream.debug.rowCount}` : ""}
            {chat.stream.debug.truncated ? " · 已截断" : ""}
          </div>
          {chat.stream.debug.sql ? <pre className="block">{chat.stream.debug.sql}</pre> : null}
        </div>
      ) : null}
    </div>
  );
}
