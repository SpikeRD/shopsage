"use client";

/**
 * 输入框（对客聊天页与对内查询页共用）。
 *
 * 三条前端该管的、与安全无关的规矩：
 *
 * 1. **长度上限与服务端一致**（2000 字，`app/api/schemas.py`）：超长时按钮禁用并显示计数，
 *    而不是让用户点一下换一个 422 —— 但那 422 依然存在（前端拦不住绕过前端的调用），
 *    所以后端那条校验**不是**冗余的；
 * 2. **`Ctrl/Cmd + Enter` 发送**（聊天框的肌肉记忆），`Enter` 换行；
 * 3. **`running` 期间禁用发送**：一轮没结束时再发会把两轮的帧串在一起
 *    （WS 是一条连接上的消息循环，见 `lib/stream.ts` 的口径 3）。
 */

import { useState } from "react";
import { MESSAGE_MAX_LENGTH } from "@/lib/contracts";

export function Composer({
  running,
  placeholder,
  submitLabel = "发送",
  onSubmit,
}: {
  running: boolean;
  placeholder?: string;
  submitLabel?: string;
  onSubmit: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const tooLong = text.length > MESSAGE_MAX_LENGTH;
  const empty = text.trim().length === 0;

  const submit = () => {
    if (running || empty || tooLong) return;
    onSubmit(text.trim());
    setText("");
  };

  return (
    <div className="stack">
      <div className="composer">
        <textarea
          aria-label="输入问题"
          className="input"
          rows={2}
          value={text}
          disabled={running}
          placeholder={placeholder ?? "例如：我的订单到哪了？ / 退货政策是怎么规定的？"}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button className="btn" type="button" disabled={running || empty || tooLong} onClick={submit}>
          {running ? "处理中…" : submitLabel}
        </button>
      </div>
      <div className="row between small muted">
        <span>Ctrl/⌘ + Enter 发送；Enter 换行</span>
        <span className={tooLong ? "badge danger" : ""}>
          {text.length} / {MESSAGE_MAX_LENGTH}
        </span>
      </div>
    </div>
  );
}
