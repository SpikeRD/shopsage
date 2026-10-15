"use client";

/**
 * 管理台（`/admin`）—— 变更 001 / T16。「管理员负责开账号」这条需求的界面。
 *
 * ## 这一页只做四件事
 *
 * ① 分流：没登录 / 令牌不是管理员 → 给出**能照做**的说明（不静默跳走）；
 * ② 列表：把账号摆出来（含**入口**与是否管理员 —— 后者由服务端按权限点算）；
 * ③ 开号：账号 + 口令 + 角色 + 主体（客户/员工 + id）；
 * ④ 停用/启用、改角色、重置口令。
 *
 * ## 三条纪律（都与后端契约对应）
 *
 * 1. **不做任何授权判断**：`canUseAdmin(role)` 只决定「要不要渲染这一页」，
 *    真正的门是后端那 4 个接口上的 `@require_permission(PERM_ADMIN)`。
 *    非管理员就算把这一页强行打开，每个请求都会拿到 404（后端的越权口径）。
 * 2. **请求体里不出现调用者的身份**：`role` 字段是「被建账号的目标角色」，
 *    不是「我是谁」—— 后者只来自令牌（`lib/api.ts` 的 `postAccount` 已经这么拼）。
 * 3. **口令绝不回显**：重置口令后响应里**没有**新口令（后端契约如此），
 *    这一页也不做任何「显示刚设的口令」的花样 —— 口令一旦进屏幕就可能进截图。
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ApiError,
  getAccounts,
  KNOWN_ROLES,
  patchAccount,
  postAccount,
  postPasswordReset,
  type AccountSummary,
} from "@/lib/api";
import { browserStorage, canUseAdmin, entryLabel, type Entry, type Session } from "@/lib/auth";
import { clearSessionPair } from "@/lib/session";
import { useSession } from "@/components/useChatStream";

const ENTRY: Entry = "admin";

type Notice = { kind: "ok" | "error"; text: string } | null;

export default function AdminPage() {
  const { session, ready } = useSession(ENTRY);

  // ⚠️ 与对客页/对内页同一条口径：**不做强制 redirect**。
  // 演示环境里「一个能读的说明页 + 一个登录入口」比「莫名其妙跳走」好排查得多。
  if (!ready) {
    return <div className="card muted">正在检查登录状态…</div>;
  }
  if (!session) {
    return (
      <div className="card">
        <h1 className="card-title">管理台：需要先登录</h1>
        <p className="muted">
          这一页调用的是管理接口（后端要求 <span className="mono">admin</span> 权限点）。
          去 <Link href="/login">登录</Link>，用管理员账号（演示账号见{" "}
          <span className="mono">scripts/seed.py</span>）。
        </p>
      </div>
    );
  }
  if (!canUseAdmin(session.role)) {
    return (
      <div className="card">
        <h1 className="card-title">管理台：当前身份不是管理员</h1>
        <p className="muted">
          你现在是 <span className="chip">{session.role}</span>（主体{" "}
          <span className="mono">{session.userId}</span>）。管理台要求{" "}
          <span className="mono">admin</span> 权限点。
          <br />
          换一个管理员账号登录即可 —— 三个入口的令牌互不覆盖，所以**不会**把你当前这个入口踢下线。
        </p>
        <div className="row">
          <Link className="btn ghost tiny" href="/login">
            去登录 / 切换身份
          </Link>
          <Link className="btn ghost tiny" href="/">
            回对客页
          </Link>
        </div>
      </div>
    );
  }
  return <AdminConsole session={session} />;
}

function AdminConsole({ session }: { session: Session }) {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [notice, setNotice] = useState<Notice>(null);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState<{ role: string; status: string }>({ role: "", status: "" });

  // 建号表单
  const [draft, setDraft] = useState({
    identifier: "",
    password: "",
    role: "customer",
    subjectType: "customer",
    subjectId: "",
  });

  const load = useCallback(async () => {
    setLoading(true);
    setNotice(null);
    try {
      const rows = await getAccounts(session.token, {
        role: filters.role || undefined,
        status: filters.status || undefined,
      });
      setAccounts(rows);
    } catch (exc) {
      setNotice({ kind: "error", text: describe(exc) });
    } finally {
      setLoading(false);
    }
  }, [filters.role, filters.status, session.token]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (action: () => Promise<unknown>, okText: string) => {
    setNotice(null);
    try {
      await action();
      setNotice({ kind: "ok", text: okText });
      await load();
    } catch (exc) {
      setNotice({ kind: "error", text: describe(exc) });
    }
  };

  const create = () => {
    const subjectId = Number(draft.subjectId);
    if (!draft.identifier.trim() || !draft.password) {
      setNotice({ kind: "error", text: "登录标识与初始口令都要填。" });
      return;
    }
    if (!Number.isInteger(subjectId) || subjectId <= 0) {
      setNotice({
        kind: "error",
        text: "主体 id 必须是正整数：客户填 users.user_id，员工填 employees.emp_id（后端会校验它真的存在）。",
      });
      return;
    }
    void run(
      () =>
        postAccount(session.token, {
          identifier: draft.identifier.trim(),
          password: draft.password,
          role: draft.role,
          subjectType: draft.subjectType,
          subjectId,
        }),
      `已开账号 ${draft.identifier.trim()}（角色 ${draft.role}）。把口令告诉本人；本页不会回显它。`,
    );
    setDraft({ ...draft, identifier: "", password: "", subjectId: "" });
  };

  const resetPassword = (account: AccountSummary) => {
    const next = window.prompt(
      `给 ${account.identifier} 设一个新口令。\n\n` +
        "⚠️ 这会立刻作废该账号**所有在途令牌**（他会掉线，需要重新登录）。\n" +
        "本页不会回显口令，请自己记好并告知本人。",
      "",
    );
    if (!next) return;
    void run(
      () => postPasswordReset(session.token, account.accountId, next),
      `已重置 ${account.identifier} 的口令（他的在途令牌已全部失效）。`,
    );
  };

  return (
    <div className="stack">
      <div className="card">
        <div className="row between">
          <div>
            <h1 className="card-title" style={{ marginBottom: 2 }}>
              账号管理
            </h1>
            <div className="small muted">
              {/* ⚠️ `session.userId` 存的是**主体标识串**（`c2` / `e1`），不是数字 user_id：
                  变更 001 起令牌的 `sub` 就是它（`lib/auth.readSession` 直接透传 `claims.sub`）。 */}
              当前身份 <span className="chip">主体={session.userId} · {session.role}</span> · 共{" "}
              {accounts.length} 个账号
            </div>
          </div>
          <div className="row">
            <button className="btn ghost tiny" type="button" disabled={loading} onClick={() => void load()}>
              {loading ? "刷新中…" : "刷新"}
            </button>
            <button
              className="btn ghost tiny"
              type="button"
              onClick={() => {
                clearSessionPair(ENTRY, browserStorage());
                window.location.assign("/login");
              }}
            >
              退出管理台
            </button>
          </div>
        </div>
      </div>

      {notice ? (
        <div className={notice.kind === "ok" ? "card" : "error-box"}>
          {notice.kind === "ok" ? <span className="badge ok">成功</span> : null} {notice.text}
        </div>
      ) : null}

      <div className="card">
        <h2 className="card-title">开账号</h2>
        <p className="muted small">
          角色决定权限点（后端 <span className="mono">ROLE_PERMISSIONS</span>）；
          主体决定「这个账号是谁」——客户填 <span className="mono">users.user_id</span>、
          员工填 <span className="mono">employees.emp_id</span>。后端会校验主体真的存在
          （账号表没有外键，那是一处已知取舍）。
        </p>
        <div className="row" style={{ marginTop: 8 }}>
          <input
            className="input"
            style={{ maxWidth: 200 }}
            placeholder="登录标识（用户名/手机号）"
            aria-label="登录标识"
            value={draft.identifier}
            onChange={(event) => setDraft({ ...draft, identifier: event.target.value })}
          />
          <input
            className="input"
            style={{ maxWidth: 200 }}
            type="password"
            placeholder="初始口令"
            aria-label="初始口令"
            value={draft.password}
            onChange={(event) => setDraft({ ...draft, password: event.target.value })}
          />
          <select
            className="input"
            style={{ maxWidth: 190 }}
            aria-label="角色"
            value={draft.role}
            onChange={(event) => setDraft({ ...draft, role: event.target.value })}
          >
            {KNOWN_ROLES.map((role) => (
              <option key={role} value={role}>
                {role}
              </option>
            ))}
          </select>
          <select
            className="input"
            style={{ maxWidth: 150 }}
            aria-label="主体类别"
            value={draft.subjectType}
            onChange={(event) => setDraft({ ...draft, subjectType: event.target.value })}
          >
            <option value="customer">客户</option>
            <option value="employee">员工</option>
          </select>
          <input
            className="input"
            style={{ maxWidth: 120 }}
            placeholder="主体 id"
            aria-label="主体 id"
            value={draft.subjectId}
            onChange={(event) => setDraft({ ...draft, subjectId: event.target.value })}
          />
          <button className="btn" type="button" onClick={create}>
            开账号
          </button>
        </div>
      </div>

      <div className="card">
        <div className="row between">
          <h2 className="card-title" style={{ marginBottom: 0 }}>
            账号列表
          </h2>
          <div className="row">
            <select
              className="input"
              style={{ maxWidth: 190 }}
              aria-label="按角色筛选"
              value={filters.role}
              onChange={(event) => setFilters({ ...filters, role: event.target.value })}
            >
              <option value="">全部角色</option>
              {KNOWN_ROLES.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
            <select
              className="input"
              style={{ maxWidth: 150 }}
              aria-label="按状态筛选"
              value={filters.status}
              onChange={(event) => setFilters({ ...filters, status: event.target.value })}
            >
              <option value="">全部状态</option>
              <option value="active">active</option>
              <option value="disabled">disabled</option>
            </select>
          </div>
        </div>

        <table className="table" style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th>标识</th>
              <th>角色</th>
              <th>主体</th>
              <th>入口</th>
              <th>状态</th>
              <th>最近登录</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((account) => (
              <tr key={account.accountId}>
                <td className="mono">{account.identifier}</td>
                <td>
                  {account.role}
                  {account.isAdmin ? <span className="badge accent" style={{ marginLeft: 6 }}>admin</span> : null}
                </td>
                <td className="mono small">
                  {account.subjectType}#{account.subjectId}
                </td>
                <td className="small">
                  {entryLabel(
                    account.entry === "admin"
                      ? "admin"
                      : account.entry === "internal"
                        ? "internal"
                        : "customer",
                  )}
                </td>
                <td>
                  <span className={`badge ${account.status === "active" ? "ok" : "danger"}`}>
                    {account.status}
                  </span>
                </td>
                <td className="small muted">{account.lastLoginAt ?? "（从未）"}</td>
                <td>
                  <div className="row">
                    <button
                      className="btn ghost tiny"
                      type="button"
                      onClick={() =>
                        void run(
                          () =>
                            patchAccount(session.token, account.accountId, {
                              status: account.status === "active" ? "disabled" : "active",
                            }),
                          account.status === "active"
                            ? `已停用 ${account.identifier}（他的在途令牌立刻失效）。`
                            : `已启用 ${account.identifier}。`,
                        )
                      }
                    >
                      {account.status === "active" ? "停用" : "启用"}
                    </button>
                    <select
                      className="input"
                      style={{ maxWidth: 170 }}
                      aria-label={`改 ${account.identifier} 的角色`}
                      value={account.role}
                      onChange={(event) =>
                        void run(
                          () =>
                            patchAccount(session.token, account.accountId, { role: event.target.value }),
                          `已把 ${account.identifier} 的角色改为 ${event.target.value}（在途令牌已失效）。`,
                        )
                      }
                    >
                      {KNOWN_ROLES.map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </select>
                    <button className="btn ghost tiny" type="button" onClick={() => resetPassword(account)}>
                      重置口令
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {accounts.length === 0 ? (
              <tr>
                <td colSpan={7} className="muted small">
                  {loading ? "加载中…" : "没有符合条件的账号。"}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
        <ul className="hint-list">
          <li>「停用 / 改角色 / 重置口令」都会**立刻作废该账号所有在途令牌**（后端 `token_version` 机制）。</li>
          <li>不能停用或降级最后一个启用的管理员 —— 那会把系统锁死（后端拒绝，界面会显示原因）。</li>
          <li>本页**不显示也不回显任何口令**；重置后请自行告知本人。</li>
        </ul>
      </div>
    </div>
  );
}

/** 把接口错误翻成一句能照做的话（区分 401/404/422 —— 三者的修法完全不同）。 */
function describe(exc: unknown): string {
  if (exc instanceof ApiError) {
    if (exc.status === 404) {
      return (
        "被拒了（后端按 404 返回越权）：要么当前令牌没有 admin 权限点，" +
        `要么目标账号不存在。request_id=${exc.requestId}`
      );
    }
    if (exc.isUnauthorized) {
      return `登录态已失效（账号可能被停用/口令被改）：请重新登录。request_id=${exc.requestId}`;
    }
    return `${exc.message}（HTTP ${exc.status}${exc.requestId ? `，request_id=${exc.requestId}` : ""}）`;
  }
  return `请求没有到达后端（没有状态码）：多半是后端没起或跨源被拦。原始错误：${String(exc)}`;
}
