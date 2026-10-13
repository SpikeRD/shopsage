"use client";

/**
 * 登录 / 切角色（`/login`）。
 *
 * ## 为什么是「粘贴令牌」而不是账号密码
 *
 * `16.web.md` §6 第 4 条写的是「接 JWT 登录，按 role 渲染入口」。但本仓
 * **没有登录接口**：第 9 步只交付了「令牌怎么被验」，签发工具是
 * `app/security/auth.py::create_access_token`（演示脚本与测试都用它）。
 * 所以这一页做的是**真实链路上最短的那一段**：
 *
 * ```
 *   uv run python -m scripts.dev_token --role internal_employee   ← 签发（服务端）
 *                    │  粘贴
 *                    ▼
 *   写入 localStorage → 后续所有请求带 Authorization: Bearer <它>
 * ```
 *
 * ⚠️ 这个页面**本身不需要后端**（它只解码令牌的 payload 用于显示，不验签）。
 * 真正需要后端的是它之后的那两页 —— 令牌不对时那两页会回 401，
 * 而这里解出来的 `role` 只决定**先给你看哪个界面**（见 `lib/auth.ts` 的模块注释）。
 *
 * ## 为什么按「入口」分开放令牌
 *
 * `shopsage.token.customer` / `shopsage.token.internal` 两把钥匙互不覆盖，
 * 所以可以「用客户身份开着对客页、同时用员工身份开对内页」——
 * 这正是验收标准第 3 条（「角色权限在前端正确分流」）要能演示的东西：
 * 同一个浏览器里两个入口的行为**由各自令牌的 role 决定**，而不是由「最后登录的是谁」决定。
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { getHealth, type HealthPayload } from "@/lib/api";
import {
  browserStorage,
  canUseInternal,
  clearSession,
  decodeJwtPayload,
  DEMO_PRESETS,
  isExpired,
  tokenKind,
  writeSession,
} from "@/lib/auth";
import { apiBase } from "@/lib/config";
import { probeFailureHint } from "@/lib/diagnostics";
import type { Entry } from "@/lib/auth";

/**
 * 令牌还剩多少分钟（负数 = 已过期）。
 *
 * ⚠️ 页面上**必须显示这个**（第一版只显示 `exp` 的绝对时间）：本仓演示令牌默认 **30 分钟**，
 * 而「签一次、想半天、回来再试」是常态 —— 不显示剩余时间的话，
 * 过期会被误判成「令牌类型不对 / 后端坏了」（本次实测就发生了这个混淆，见 runbook §10.1 坑 13）。
 */
function remainingMinutes(exp: number, now: number = Date.now()): number {
  if (!exp) return 0;
  return Math.round((exp * 1000 - now) / 60000);
}

export default function LoginPage() {
  const [tokenText, setTokenText] = useState("");
  const [entry, setEntry] = useState<Entry>("customer");
  const [message, setMessage] = useState("");
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [healthError, setHealthError] = useState("");
  const [now, setNow] = useState("");

  const claims = decodeJwtPayload(tokenText.trim());
  const expired = claims ? isExpired(tokenText.trim()) : false;
  const kind = claims ? tokenKind(tokenText.trim()) : "unknown";
  const wrongType = claims ? kind !== "access" : false;

  useEffect(() => {
    setNow(new Date().toLocaleString());
  }, []);

  const save = (entryKey: Entry) => {
    const value = tokenText.trim();
    if (!value) {
      setMessage("先把令牌粘贴进来。");
      return;
    }
    if (!claims) {
      setMessage("这不是一个可解析的 JWT（应当是 `header.payload.signature` 三段）。");
      return;
    }
    if (wrongType) {
      // ⚠️ 「声明是 refresh」与「没有类型声明」要分开说（§10.1 坑 13）：
      // 前者的修法是「别加 --kind refresh」，后者是「这个令牌不是本仓签的」。
      // 第一版把两者混成一句「这是刷新令牌」，于是**真的访问令牌**被误判时，
      // 提示语会让人反复重签同一个（正确的）令牌 —— 那正是这次实测踩的坑。
      setMessage(
        kind === "refresh"
          ? "这是刷新令牌（`typ=refresh`），后端会当 401 处理 —— 默认签发出来的就是访问令牌（`--kind refresh` 才会得到这种）。"
          : "这个令牌没有类型声明（payload 里既没有 `typ` 也没有 `type`）：它多半不是本仓 `scripts/dev_token.py` 签的，" +
            "或者被截断了。后端对「类型缺失」也是拒绝，所以请用 `uv run python -m scripts.dev_token` 重签一个。",
      );
      return;
    }
    if (expired) {
      setMessage("这个令牌已经过期了，请重新签发。");
      return;
    }
    writeSession({ token: value, userId: claims.sub, role: claims.role, entry: entryKey }, browserStorage());
    setMessage(`已保存到「${entryKey === "customer" ? "对客" : "对内"}」入口（role=${claims.role}）。`);
  };

  const logout = (entryKey: Entry) => {
    clearSession(entryKey, browserStorage());
    setMessage(`已清除「${entryKey === "customer" ? "对客" : "对内"}」入口的登录态。`);
  };

  const probe = async () => {
    setHealthError("");
    setHealth(null);
    try {
      const payload = await getHealth();
      setHealth(payload);
    } catch (exc) {
      setHealthError(probeFailureHint(exc, { apiBase: apiBase() }));
    }
  };

  return (
    <div className="stack">
      <div className="card">
        <h1 className="card-title">登录 / 切角色</h1>
        <p className="muted small">
          本仓没有登录接口（第 9 步只交付「令牌怎么被验」）。用一条命令签一个演示令牌，
          粘到这里即可：<span className="mono">uv run python -m scripts.dev_token --role internal_employee</span>
          （脚本会打印命令与令牌；`--kind refresh` 可以故意签一个刷新令牌来试 401 的分支）。
        </p>
        <div className="row small muted">
          <span>
            当前后端：<span className="mono">{apiBase()}</span>
          </span>
          <span>页面时间：{now || "…"}</span>
          <button className="btn ghost tiny" type="button" onClick={() => void probe()}>
            探活 /health
          </button>
        </div>
        {health ? (
          <div className="small" style={{ marginTop: 8 }}>
            <span className={`badge ${health.status === "ok" ? "ok" : "warn"}`}>status={health.status}</span>{" "}
            <span className="muted">
              env={health.env} version={health.version} ·{" "}
              {health.components.map((item) => `${item.name}=${item.status}`).join(" · ")}
            </span>
          </div>
        ) : null}
        {healthError ? <div className="error-box" style={{ marginTop: 8 }}>{healthError}</div> : null}
      </div>

      <div className="card">
        <h2 className="card-title">① 粘贴令牌</h2>
        <textarea
          className="input"
          rows={4}
          aria-label="访问令牌"
          placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.…"
          value={tokenText}
          onChange={(event) => setTokenText(event.target.value)}
        />
        <div className="row small" style={{ marginTop: 8 }}>
          {claims ? (
            <>
              <span className="badge accent">sub={claims.sub}</span>
              <span className="badge">role={claims.role}</span>
              <span className={`badge ${wrongType ? "danger" : "ok"}`}>typ={claims.tokenType || "(未标注)"}</span>
              <span className={`badge ${expired ? "danger" : "ok"}`}>
                exp={claims.exp ? new Date(claims.exp * 1000).toLocaleString() : "缺失"}
              </span>
              <span className="badge">剩余 {remainingMinutes(claims.exp)} 分钟</span>
              <span className="muted">
                （解出来的角色只用来选界面；它不验签，因此不能当权限依据）
              </span>
            </>
          ) : (
            <span className="muted">
              还没有可解析的令牌。解不开的原因通常是：复制时缺了 `.` 之间的某一段。
            </span>
          )}
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <select
            className="input"
            style={{ maxWidth: 200 }}
            aria-label="保存到哪个入口"
            value={entry}
            onChange={(event) => setEntry(event.target.value as Entry)}
          >
            <option value="customer">保存到：对客入口</option>
            <option value="internal">保存到：对内入口</option>
          </select>
          <button className="btn" type="button" onClick={() => save(entry)}>
            保存
          </button>
          <button className="btn ghost" type="button" onClick={() => logout("customer")}>
            退出对客
          </button>
          <button className="btn ghost" type="button" onClick={() => logout("internal")}>
            退出对内
          </button>
        </div>
        {message ? <p className="small" style={{ marginBottom: 0 }}>{message}</p> : null}
      </div>

      <div className="card">
        <h2 className="card-title">② 演示身份（一键跳转）</h2>
        <p className="muted small">
          下面四张卡只是**提示**：签令牌时用对应的 `--role` / `--user-id`，保存后点卡片上的链接进对应入口。
          「对内」入口只有能读全站订单的角色才进得去（客户角色点它会被提示 —— 那是前端分流，
          真正的拦截在后端）。
        </p>
        <div className="login-grid">
          {DEMO_PRESETS.map((preset) => (
            <div className="login-card" key={`${preset.entry}-${preset.role}`}>
              <strong>{preset.label}</strong>
              <span className="small muted">{preset.hint}</span>
              <span className="mono small">
                --role {preset.role} --user-id {preset.userId}
              </span>
              <div className="row">
                <Link className="btn ghost tiny" href={preset.entry === "customer" ? "/" : "/internal"}>
                  打开{preset.entry === "customer" ? "对客" : "对内"}页
                </Link>
                {canUseInternal(preset.role) ? <span className="badge ok">可进对内</span> : <span className="badge">仅对客</span>}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
