"use client";

/**
 * 登录页（`/login`）—— 变更 002 / T8 把它**瘦到只剩登录**。
 *
 * ## 变更 002 从这里删掉了什么，为什么
 *
 * 变更 001 的登录页有**四块**：①账号+口令 ②三个入口的登录态卡片 ③开发模式粘贴令牌
 * ④演示身份卡片。需求原文是「登录界面要简洁，只管登录账户」，所以 ②③④ 全部删除：
 *
 * | 删掉的 | 它原来解决什么 | 删了之后怎么办 |
 * |---|---|---|
 * | ② 登录态卡片 | 看「三个入口各登录了谁」 | 主界面侧边栏底部会显示**当前入口**的个人信息与权限状态（`SideNav.tsx`） |
 * | ③ 开发模式粘贴令牌 | 排障 / `tests/live` 用现签的令牌 | 排障改在 DevTools 里 `localStorage.setItem("shopsage.token.customer", "<token>")`（见变更单 002 §3.3） |
 * | ④ 演示身份卡片 | 提示演示账号 | **登录页不再展示任何演示账号标识**（那既是"不简洁"，也是一处信息暴露） |
 *
 * ⚠️ **存储层能力一个都没动**：`lib/session.ts` 的「三把钥匙互不覆盖」
 * （`shopsage.token.customer` / `.internal` / `.admin`）仍然成立，只是**界面上**
 * 不再有管理它的入口。这是刻意的取舍（变更单 002 §8 遗留 L1）。
 *
 * ## 两条保留的设计（都是**安全相关**的，不能顺手删）
 *
 * 1. **入口由服务端返回**：登录成功后按 `payload.entry` 跳转，前端不猜
 *    （`lib/session.ts::writeSessionPair` 会把它写进存储）。变更 001 建立的
 *    「入口分流只有一处真相」继续有效。
 * 2. **不区分「账号不存在」与「口令错误」**：后端 `accounts.authenticate` 刻意
 *    返回同一句话（防账号枚举），前端也不许把两者拆开显示 —— 那会让后端的
 *    防枚举工作白做。
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError, postLogin } from "@/lib/api";
import { browserStorage, entryLabel, entryPath } from "@/lib/auth";
import { apiBase } from "@/lib/config";
import { writeSessionPair } from "@/lib/session";

export default function LoginPage() {
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [apiBaseText, setApiBaseText] = useState("");

  // 后端地址在**客户端**才读得到：`NEXT_PUBLIC_*` 是构建期字面替换，但读它本身
  // 要放在 effect 里，避免 SSR 与客户端渲染出不同的文本（水合不一致）。
  useEffect(() => {
    setApiBaseText(apiBase());
  }, []);

  /** 账号 + 口令登录。成功后按**服务端返回的 entry** 跳转。 */
  const login = useCallback(async () => {
    setError("");
    setMessage("");
    if (!identifier.trim() || !password) {
      setError("账号和口令都要填。");
      return;
    }
    setBusy(true);
    try {
      const payload = await postLogin(identifier.trim(), password);
      const entry = writeSessionPair(payload, browserStorage());
      setMessage(`登录成功：${payload.subject} · ${payload.role} · 进入「${entryLabel(entry)}」…`);
      // ⚠️ 用 `location.assign` 而不是 `router.push`：本页是**登录入口**，
      // 整页跳转能把上一轮的组件状态（错误提示、busy 标志）彻底清掉。
      // 用 push 时「登录 → 返回 → 再登录」会看到上一个账号的残留提示。
      window.location.assign(entryPath(entry));
    } catch (exc) {
      setError(loginFailureHint(exc));
    } finally {
      setBusy(false);
    }
  }, [identifier, password]);

  return (
    <div className="card">
      <h1 className="card-title">登录</h1>
      <p className="muted small">
        用账号 + 口令登录。进入哪个界面由服务端按你的角色决定
        （管理员 → 管理台、能读全站订单 → 对内查询、其余 → 对客问答）。
      </p>

      <div className="stack" style={{ marginTop: 12 }}>
        <label className="small muted" htmlFor="login-identifier">
          账号
        </label>
        <input
          id="login-identifier"
          className="input"
          placeholder="员工用户名 或 客户手机号"
          autoComplete="username"
          autoFocus
          value={identifier}
          onChange={(event) => setIdentifier(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void login();
          }}
        />

        <label className="small muted" htmlFor="login-password">
          口令
        </label>
        <input
          id="login-password"
          className="input"
          type="password"
          placeholder="口令"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void login();
          }}
        />

        <button className="btn" type="button" disabled={busy} onClick={() => void login()}>
          {busy ? "登录中…" : "登录"}
        </button>
      </div>

      {message ? (
        <p className="small" style={{ marginTop: 10 }}>
          {message}
        </p>
      ) : null}
      {error ? (
        <div className="error-box" style={{ marginTop: 10 }}>
          {error}
        </div>
      ) : null}

      <ul className="hint-list">
        <li>口令错误与账号不存在返回**同一句话** —— 那是防账号枚举，不是提示没写清楚。</li>
        <li>访问令牌 30 分钟；前端会在快过期时自动续期，不需要手动重登。</li>
        {apiBaseText ? (
          <li>
            当前后端：<span className="mono">{apiBaseText}</span>
          </li>
        ) : null}
      </ul>
    </div>
  );
}

/**
 * 把登录失败翻成一句**能照做**的话。
 *
 * ⚠️ 三条不许混：401（凭据不对）、429（试太多次）、网络/跨源失败（`Failed to fetch`）。
 * 它们的修法完全不同 —— 变更 001 的登录页正好踩过这个坑（把「令牌类型不对」与
 * 「令牌声明缺失」混成一句，见 `16-web-runbook` §10.1 坑 13）。
 */
function loginFailureHint(exc: unknown): string {
  if (exc instanceof ApiError) {
    if (exc.status === 429) {
      return `登录尝试过于频繁（限流）：请等 ${exc.retryAfter ?? 60} 秒再试。这条限流是防暴力破解的。`;
    }
    if (exc.isUnauthorized) {
      return `账号或口令不正确（服务端刻意不区分这两种原因，防账号枚举）。request_id=${exc.requestId}`;
    }
    return `${exc.message}（HTTP ${exc.status}${exc.requestId ? `，request_id=${exc.requestId}` : ""}）`;
  }
  // 没有状态码的失败 = 请求的结果没被读到（服务没起 / 跨源被拦 / 网络）
  return (
    "请求没有到达后端（没有 HTTP 状态码）：多半是后端没起、或跨源被浏览器拦掉。" +
    `页面顶部的探活横幅会说明具体原因。原始错误：${String(exc)}`
  );
}
