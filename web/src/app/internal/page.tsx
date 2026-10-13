"use client";

/**
 * 对内入口（`/internal`）：数据查询 / 报表 —— **走 HTTP（非流式）而不是 WebSocket**。
 *
 * ## 为什么这一页不用流式（一个刻意的取舍）
 *
 * WebSocket 的 `answer` 帧只带一个布尔 `chart`（`app/api/chat.py`：帧里不塞图表本体），
 * 而图表 spec 只出现在 **HTTP 响应**的 `chart_spec` 字段里。也就是说：
 *
 * * 要图表 → 必须走 `POST /api/chat`；
 * * 要流式 → 只能拿到「有没有图」这个布尔。
 *
 * 两者都要的话得改后端协议（在 `answer` 帧里带上 spec，或者新增一种帧）——
 * 那超出第 16 步的边界（本步是**演示层**，不改后端契约）。所以这里的选择是：
 * **对内页要的是「结果」（表格 + 图表 + 可审计的 spec），不是「首字快」**，
 * 于是走 HTTP；对客页要的是「首字快 + 可中断」，于是走 WS。
 * 这条分工写在 runbook §4.2，将来若要给图表加流式，改的是后端帧 + 这里的 hook。
 *
 * ## 门禁
 *
 * 页面在渲染数据之前先看角色：能读全站订单的角色才显示查询框，否则给一句说明 +
 * 一个去登录的链接。**这不是安全边界**（`lib/auth.ts` 的注释写了）：真正的拦截在后端
 * ——越权请求会得到 404/403，这一页只是不把「你点不动的按钮」画出来。
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ApiError, postChat } from "@/lib/api";
import { canUseInternal, ROLE_PERMISSIONS } from "@/lib/auth";
import { chartWarnings, toEChartsOption, toTableView } from "@/lib/chart";
import type { ChatResponse, ChartSpec } from "@/lib/contracts";
import { apiBase } from "@/lib/config";
import { Composer } from "@/components/Composer";
import { EChart } from "@/components/EChart";
import { useSession } from "@/components/useChatStream";

const SUGGESTIONS = [
  "最近 30 天每天的订单金额趋势，用折线图",
  "各个订单状态的订单数量，用柱状图",
  "销售额最高的 10 个商品",
  "本月退款金额按商品分类的占比，用饼图",
];

export default function InternalPage() {
  const { session, ready } = useSession("internal");
  if (!ready) return <div className="card muted">正在检查登录状态…</div>;
  if (!session) {
    return (
      <div className="card">
        <h1 className="card-title">对内入口：需要先登录</h1>
        <p className="muted">
          这一页用**内部角色**的令牌调用同一个接口（`POST /api/chat`），差别只在令牌里的{" "}
          <span className="mono">role</span> —— 后端据此放行全站订单查询、并按敏感度脱敏字段。
        </p>
        <p>
          <Link className="btn ghost tiny" href="/login">
            去登录 / 生成令牌
          </Link>
        </p>
      </div>
    );
  }
  return <InternalConsole token={session.token} role={session.role} userId={session.userId} />;
}

function InternalConsole({ token, role, userId }: { token: string; role: string; userId: string }) {
  const [response, setResponse] = useState<ChatResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [showSpec, setShowSpec] = useState(false);
  const [showTable, setShowTable] = useState(true);
  const allowed = canUseInternal(role);

  // 会话 id（HTTP 侧要显式带，才能接上多轮）：与聊天页共用同一个键前缀，但独立一份
  const [sessionId, setSessionId] = useState<string | null>(null);
  useEffect(() => {
    const key = "shopsage.session.internal";
    let value = window.localStorage.getItem(key);
    if (!value) {
      value = `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
      window.localStorage.setItem(key, value);
    }
    setSessionId(value);
  }, []);

  const ask = async (text: string) => {
    setRunning(true);
    setError("");
    try {
      const payload = await postChat(token, { message: text, sessionId });
      setResponse(payload);
      if (payload.session_id) setSessionId(payload.session_id);
    } catch (exc) {
      if (exc instanceof ApiError) {
        setError(
          `${exc.message}（HTTP ${exc.status} · code=${exc.code}${exc.requestId ? ` · request_id=${exc.requestId}` : ""}）`,
        );
      } else {
        setError(String(exc));
      }
      setResponse(null);
    } finally {
      setRunning(false);
    }
  };

  const chartSpec: ChartSpec | null = response?.chart_spec ?? null;
  const warnings = useMemo(() => (chartSpec ? chartWarnings(chartSpec) : []), [chartSpec]);
  const option = useMemo(() => {
    if (!chartSpec || chartSpec.type === "table") return null;
    try {
      return toEChartsOption(chartSpec);
    } catch {
      return null;
    }
  }, [chartSpec]);
  const table = useMemo(() => (chartSpec ? toTableView(chartSpec) : null), [chartSpec]);

  return (
    <div className="stack">
      <div className="card">
        <div className="row between">
          <div>
            <h1 className="card-title" style={{ marginBottom: 2 }}>
              对内数据查询 / 报表
            </h1>
            <div className="small muted">
              后端 <span className="mono">{apiBase()}</span> · 身份{" "}
              <span className="chip">
                user_id={userId} · {role}
              </span>{" "}
              · 会话 <span className="chip">{sessionId ?? "（生成中）"}</span>
            </div>
          </div>
          <div className="row">
            <span className={`badge ${allowed ? "ok" : "warn"}`}>
              {allowed ? "可读全站订单" : "无权读全站订单"}
            </span>
            <span className="badge">权限点 {(ROLE_PERMISSIONS[role] ?? []).length} 个</span>
            <Link className="btn ghost tiny" href="/">
              切到对客问答
            </Link>
          </div>
        </div>
        {!allowed ? (
          <div className="error-box" style={{ marginTop: 10 }}>
            当前角色（<span className="mono">{role}</span>）没有{" "}
            <span className="mono">read:all_orders</span> 权限点：这一页的查询框已禁用。
            这是**前端分流**；即便绕过它直接调接口，后端也会按 RBAC 拒绝（对客路径还会被行级过滤限制在
            <span className="mono"> user_id={userId} </span>）。
          </div>
        ) : null}
      </div>

      <div className="card">
        <Composer
          running={running || !allowed}
          submitLabel="查询"
          placeholder="例如：最近 30 天每天的订单金额趋势，用折线图"
          onSubmit={(text) => void ask(text)}
        />
        <div className="row" style={{ marginTop: 10 }}>
          {SUGGESTIONS.map((text) => (
            <button
              className="btn ghost tiny"
              key={text}
              type="button"
              disabled={running || !allowed}
              onClick={() => void ask(text)}
            >
              {text}
            </button>
          ))}
        </div>
      </div>

      {error ? <div className="error-box">{error}</div> : null}

      {response ? (
        <div className="card">
          <div className="row between">
            <h2 className="card-title">回答</h2>
            <div className="row">
              <span className={`badge ${response.blocked ? "danger" : "ok"}`}>flow_state={response.flow_state}</span>
              {response.route ? <span className="badge accent">路径 {response.route}</span> : null}
              {response.debug?.rowCount !== undefined && response.debug?.rowCount !== null ? (
                <span className="badge">行数 {response.debug.rowCount}</span>
              ) : null}
              {response.debug?.truncated ? <span className="badge warn">已截断</span> : null}
            </div>
          </div>
          <p style={{ whiteSpace: "pre-wrap" }}>{response.answer || "（这一轮没有回答）"}</p>
          {response.refusals.length > 0 ? (
            <ul className="hint-list">
              {response.refusals.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
          {response.citations.length > 0 ? (
            <div className="citations">
              <div className="small muted">来源：</div>
              {response.citations.map((item) => (
                <div className="citation-item" key={item}>
                  <Link href={`/citation?q=${encodeURIComponent(item)}`}>{item}</Link>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {chartSpec ? (
        <div className="card">
          <div className="row between">
            <h2 className="card-title">
              图表 · {chartSpec.title}{" "}
              <span className="badge">{chartSpec.type}</span>{" "}
              <span className="badge">{chartSpec.schema}</span>
            </h2>
            <div className="row">
              <button className="btn ghost tiny" type="button" onClick={() => setShowTable((value) => !value)}>
                {showTable ? "隐藏数据表" : "显示数据表"}
              </button>
              <button className="btn ghost tiny" type="button" onClick={() => setShowSpec((value) => !value)}>
                {showSpec ? "隐藏 spec" : "查看 spec（JSON）"}
              </button>
            </div>
          </div>

          {option ? <EChart option={option} /> : <p className="muted small">`table` 类型不画图，直接看下面的数据表。</p>}

          {warnings.length > 0 ? (
            <ul className="hint-list">
              {warnings.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}

          {table && showTable ? (
            <div className="scroll-x" style={{ marginTop: 10 }}>
              <table className="data">
                <thead>
                  <tr>
                    {table.columns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.rows.map((row, index) => (
                    <tr key={index}>
                      {row.map((cell, cellIndex) => (
                        <td key={cellIndex}>{cell === null ? "—" : String(cell)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          {showSpec ? <pre className="block">{JSON.stringify(chartSpec, null, 2)}</pre> : null}
        </div>
      ) : response ? (
        <div className="card muted small">
          这一轮没有 `chart_spec`：可能是问题不需要图表，也可能是数据形状不适合画图
          （`app/agents/analysis.py` 在列名/类型不合适时会记一条 caveats 并置空 spec）。
        </div>
      ) : null}

      {response?.debug?.sql ? (
        <div className="card">
          <h2 className="card-title small">本轮执行的 SQL（内部角色 + API_DEBUG_PAYLOAD=1 时才有）</h2>
          <pre className="block">{response.debug.sql}</pre>
          <div className="small muted">
            走过节点：{response.debug.visited.join(" → ") || "（无）"}
          </div>
        </div>
      ) : null}
    </div>
  );
}
