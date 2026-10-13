"use client";

/**
 * 流式状态条：**把「这一轮走到哪了」如实显示出来**（B2 的可见部分）。
 *
 * 显示三样东西，各自都有理由：
 *
 * 1. **骨架提示语**（`skeleton` 帧的文本）：首帧一到就有话可说，
 *    用户看到的是「正在理解你的问题…」而不是一个转圈的空白；
 * 2. **走过的图节点**（`node` 帧）：它既是排障信息，也是「系统真的在干活」的证据。
 *    ⚠️ 节点名来自 `NODE_PROGRESS` 的映射（后端给的 `progress` 文案），
 *    前端**不翻译**节点名 —— 那会让「界面上显示的节点」与「日志里的节点」对不上；
 * 3. **首帧耗时**：服务端自报的 `first_frame_ms`（`done` 帧里）。
 *    显示服务端的数字而不是浏览器算的（前者才是 B2 的判据，
 *    后者含网络与渲染开销，会把「后端快了」显示成「后端慢」）。
 */

import type { StreamState } from "@/lib/stream";

export function StreamStatus({ state }: { state: StreamState }) {
  const streaming = state.phase === "streaming" || state.phase === "connecting";
  const progress = state.skeleton[state.skeleton.length - 1] ?? "";
  if (!streaming && state.nodes.length === 0) return null;

  return (
    <div className="progress-line" data-testid="stream-status">
      {streaming ? (
        <span className="dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
      ) : null}
      <span>{streaming ? progress || "正在连接…" : "本轮结束"}</span>
      {state.skeleton.length > 1 ? (
        <span className="muted small">（骨架更新 {state.skeleton.length} 次）</span>
      ) : null}
      <span className="node-track">
        {state.nodes.map((node) => (
          <span className="badge" key={node} title={state.nodeProgress[node] || node}>
            {node}
          </span>
        ))}
      </span>
      {state.serverFirstFrameMs !== null ? (
        <span className="muted small">首帧 {state.serverFirstFrameMs}ms（服务端自报）</span>
      ) : null}
      {state.latencyMs !== null ? <span className="muted small">整轮 {state.latencyMs}ms</span> : null}
    </div>
  );
}
