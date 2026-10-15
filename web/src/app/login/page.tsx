"use client";

/**
 * 登录页（`/login`）—— 变更 001 / T15。
 *
 * ## 这一页在变更 001 里换掉了什么
 *
 * | | 变更前 | 变更后 |
 * |---|---|---|
 * | 怎么拿到令牌 | **粘贴**（`scripts/dev_token.py` 现场签发） | **账号 + 口令**（`POST /api/auth/login`） |
 * | 谁决定进哪个界面 | 前端按角色**猜**（`defaultEntry`） | **服务端返回 `entry`** |
 * | 30 分钟之后 | 重新签一个、重新粘 | 自动用刷新令牌续期（`lib/session.ts`） |
 * | 粘贴令牌 | 唯一的方式 | 收进「开发模式」折叠区（**不删**：live 用例与排障还靠它） |
 *
 * ## 两条保留的设计
 *
 * 1. **三把钥匙互不覆盖**：`shopsage.token.customer` / `.internal` / `.admin`
 *    —— 可以「用客户身份开着对客页、同时用管理员身份开着管理台」。
 *    同一个浏览器里两个入口的行为各由自己令牌的 `role` 决定，不由「最后登录的是谁」决定。
 * 2. **解出来的角色只用来选界面**：`decodeJwtPayload` 不验签（浏览器没有密钥），
 *    所以它**不能**当权限依据。真正的门在服务端（见 `lib/auth.ts` 的模块注释）。
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, getHealth, postLogin, type HealthPayload } from "@/lib/api";
import {
  browserStorage,
  canUseAdmin,
  canUseInternal,
  decodeJwtPayload,
  DEMO_PRESETS,
  entryLabel,
  entryPath,
  type Entry,
  isAccountToken,
  isExpired,
  tokenKind,
  writeSession,
} from "@/lib/auth";
import {
  clearSessionPair,
  readActiveSession,
  writeSessionPair,
  type ActiveSession,
} from "@/lib/session";
import { apiBase } from "@/lib/config";
import { probeFailureHint } from "@/lib/diagnostics";

/** 令牌还剩多少分钟（负数 = 已过期）。 */
function remainingMinutes(exp: number, now: number = Date.now()): number {
  if (!exp) return 0;
  return Math.round((exp * 1000 - now) / 60000);
}

const ENTRIES: Entry[] = ["customer", "internal", "admin"];

export default function LoginPage() {
  // ── 账号登录（主路径）──────────────────────────────────────────────────────
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  // ── 开发模式：粘贴令牌 ─────────────────────────────────────────────────────
  const [showDevMode, setShowDevMode] = useState(false);
  const [tokenText, setTokenText] = useState("");
  const [devEntry, setDevEntry] = useState<Entry>("customer");

  // ── 现场 ───────────────────────────────────────────────────────────────────
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [healthError, setHealthError] = useState("");
  const [now, setNow] = useState("");
  const [sessions, setSessions] = useState<Record<string, ActiveSession | null>>({});

  const refreshSessions = useCallback(() => {
    const storage = browserStorage();
    const next: Record<string, ActiveSession | null> = {};
    for (const entry of ENTRIES) next[entry] = readActiveSession(entry, storage);
    setSessions(next);
  }, []);

  useEffect(() => {
    setNow(new Date().toLocaleString());
    refreshSessions();
  }, [refreshSessions]);

  const claims = decodeJwtPayload(tokenText.trim());
  const expired = claims ? isExpired(tokenText.trim()) : false;
  const kind = claims ? tokenKind(tokenText.trim()) : "unknown";
  const wrongType = claims ? kind !== "access" : false;

  /** 账号 + 口令登录。成功后按**服务端返回的 entry** 跳转。 */
  const login = async () => {
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
      refreshSessions();
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
  };

  const saveToken = (entryKey: Entry) => {
    const value = tokenText.trim();
    if (!value) {
      setError("先把令牌粘贴进来。");
      return;
    }
    if (!claims) {
      setError("这不是一个可解析的 JWT（应当是 `header.payload.signature` 三段）。");
      return;
    }
    if (wrongType) {
      setError(
        kind === "refresh"
          ? "这是刷新令牌（`typ=refresh`），后端会当 401 处理 —— 默认签发出来的就是访问令牌。"
          : "这个令牌没有类型声明（payload 里既没有 `typ` 也没有 `type`）：它多半不是本仓脚本签的，或者被截断了。",
      );
      return;
    }
    if (expired) {
      setError("这个令牌已经过期了，请重新签发。");
      return;
    }
    // ⚠️ 旧格式令牌（变更 001 之前签的）**必然被后端拒**，在这里就说清楚：
    // 否则用户会拿着它去发消息、拿到一个 401，然后去查「是不是后端坏了」。
    if (!isAccountToken(value)) {
      setError(
        "这个令牌是**旧格式**（没有账号/世代号声明）：变更 001 之后它一定被后端拒。" +
          "请用账号+口令登录，或用 `uv run python -m scripts.dev_token` 重签一个。",
      );
      return;
    }
    writeSession(
      { token: value, userId: claims.sub, role: claims.role, entry: entryKey },
      browserStorage(),
    );
    refreshSessions();
    setMessage(
      `已保存到「${entryLabel(entryKey)}」（role=${claims.role}，主体=${claims.sub}）。` +
        "注意：这种方式没有刷新令牌，30 分钟后需要重新粘贴。",
    );
  };

  const logout = (entryKey: Entry) => {
    clearSessionPair(entryKey, browserStorage());
    refreshSessions();
    setMessage(`已清除「${entryLabel(entryKey)}」的登录态。`);
  };

  const probe = async () => {
    setHealthError("");
    setHealth(null);
    try {
      setHealth(await getHealth());
    } catch (exc) {
      setHealthError(probeFailureHint(exc, { apiBase: apiBase() }));
    }
  };

  return (
    <div className="stack">
      <div className="card">
        <h1 className="card-title">登录</h1>
        <p className="muted small">
          用**账号 + 口令**登录（变更 001 起后端有真登录接口）。成功后按你的角色进对应界面：
          管理员 → 管理台、能读全站订单 → 对内查询、其余 → 对客问答。
          「进哪个界面」由**服务端**在登录响应里给定，前端不猜。
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
        {healthError ? (
          <div className="error-box" style={{ marginTop: 8 }}>
            {healthError}
          </div>
        ) : null}
      </div>

      <div className="card">
        <h2 className="card-title">① 账号登录</h2>
        <div className="row" style={{ marginTop: 8 }}>
          <input
            className="input"
            style={{ maxWidth: 280 }}
            aria-label="账号"
            placeholder="员工用户名 或 客户手机号"
            autoComplete="username"
            value={identifier}
            onChange={(event) => setIdentifier(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void login();
            }}
          />
          <input
            className="input"
            style={{ maxWidth: 280 }}
            type="password"
            aria-label="口令"
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
        <ul className="hint-list">
          <li>
            演示账号见 <span className="mono">scripts/seed.py</span> 的{" "}
            <span className="mono">DEMO_ACCOUNTS</span>（口令刻意不打印在终端里）。
          </li>
          <li>「账号不存在」与「口令错误」返回**同一句话** —— 那是防枚举，不是提示没写清楚。</li>
          <li>访问令牌 30 分钟；前端会在快过期时自动续期，不需要你手动重登。</li>
        </ul>
        {message ? <p className="small">{message}</p> : null}
        {error ? <div className="error-box">{error}</div> : null}
      </div>

      <div className="card">
        <h2 className="card-title">② 当前登录态（三个入口各一把钥匙）</h2>
        <p className="muted small">
          三个入口的令牌**互不覆盖**：可以「用客户身份开着对客页、同时用管理员身份开着管理台」。
          每一行只反映那个入口自己的令牌。
        </p>
        <div className="login-grid">
          {ENTRIES.map((entry) => {
            const session = sessions[entry];
            return (
              <div className="login-card" key={entry}>
                <strong>{entryLabel(entry)}</strong>
                {session ? (
                  <>
                    <span className="badge ok">已登录</span>
                    <span className="small muted mono">
                      主体={session.subject} · {session.role}
                    </span>
                    <div className="row">
                      <Link className="btn ghost tiny" href={entryPath(entry)}>
                        打开
                      </Link>
                      <button className="btn ghost tiny" type="button" onClick={() => logout(entry)}>
                        退出
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <span className="badge">未登录</span>
                    <span className="small muted">
                      {entry === "admin"
                        ? "只有带 admin 权限点的角色进得去"
                        : entry === "internal"
                          ? "要能读全站订单"
                          : "客户入口"}
                    </span>
                  </>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <h2 className="card-title">
          ③ 开发模式：粘贴令牌
          <button
            className="btn ghost tiny"
            type="button"
            style={{ marginLeft: 10 }}
            onClick={() => setShowDevMode((value) => !value)}
          >
            {showDevMode ? "收起" : "展开"}
          </button>
        </h2>
        {showDevMode ? (
          <>
            <p className="muted small">
              变更 001 之后**不需要**这样登录（有真登录接口了）。这条路保留给排障与真链路用例：
              <span className="mono"> uv run python -m scripts.dev_token --account admin</span>
              <br />
              ⚠️ 它**没有刷新令牌**，30 分钟后要重新粘贴；而且变更 001 之前的旧格式令牌后端一定拒
              （这里也会提前提示你）。
            </p>
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
                  <span className="badge accent">主体={claims.sub}</span>
                  <span className="badge">role={claims.role}</span>
                  <span className={`badge ${isAccountToken(tokenText.trim()) ? "ok" : "danger"}`}>
                    {isAccountToken(tokenText.trim()) ? "新格式" : "旧格式（会被拒）"}
                  </span>
                  <span className={`badge ${wrongType ? "danger" : "ok"}`}>
                    typ={claims.tokenType || "(未标注)"}
                  </span>
                  <span className={`badge ${expired ? "danger" : "ok"}`}>
                    剩余 {remainingMinutes(claims.exp)} 分钟
                  </span>
                </>
              ) : (
                <span className="muted">还没有可解析的令牌（多半是复制时缺了某一段）。</span>
              )}
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <select
                className="input"
                style={{ maxWidth: 220 }}
                aria-label="保存到哪个入口"
                value={devEntry}
                onChange={(event) => setDevEntry(event.target.value as Entry)}
              >
                {ENTRIES.map((entry) => (
                  <option key={entry} value={entry}>
                    保存到：{entryLabel(entry)}
                  </option>
                ))}
              </select>
              <button className="btn" type="button" onClick={() => saveToken(devEntry)}>
                保存
              </button>
              <button className="btn ghost" type="button" onClick={() => logout(devEntry)}>
                清除
              </button>
            </div>
          </>
        ) : (
          <p className="muted small">已收起。展开后可以粘贴 `scripts/dev_token` 签出的令牌。</p>
        )}
      </div>

      <div className="card">
        <h2 className="card-title">④ 演示身份（一键看说明）</h2>
        <p className="muted small">
          下面几张卡只是**提示**：签令牌时用对应的 `--account`，或者直接用账号+口令登录。
          「入口」那一列是服务端的分流结果 —— 客户角色点管理台会被提示，真正的拦截在后端。
        </p>
        <div className="login-grid">
          {DEMO_PRESETS.map((preset) => (
            <div className="login-card" key={`${preset.entry}-${preset.role}`}>
              <strong>{preset.label}</strong>
              <span className="small muted">{preset.hint}</span>
              <span className="mono small">--account {preset.account}</span>
              <div className="row">
                <span className="badge">{entryLabel(preset.entry)}</span>
                {canUseAdmin(preset.role) ? (
                  <span className="badge ok">可进管理台</span>
                ) : canUseInternal(preset.role) ? (
                  <span className="badge ok">可进对内</span>
                ) : (
                  <span className="badge">仅对客</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * 把登录失败翻成一句**能照做**的话。
 *
 * ⚠️ 三条不许混：401（凭据不对）、429（试太多次）、网络/跨源失败（`Failed to fetch`）。
 * 它们的修法完全不同 —— 变更前的登录页正好踩过这个坑（把「令牌类型不对」与
 * 「令牌声明缺失」混成一句，见 16-web-runbook §10.1 坑 13）。
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
    `先跑 uv run python -m scripts.check_web_cors 自查。原始错误：${String(exc)}`
  );
}
