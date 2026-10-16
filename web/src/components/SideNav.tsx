"use client";

/**
 * 左侧边栏（变更 002 / T5）：**入口 + 个人信息 + 权限状态**三合一。
 *
 * 它取代了变更 001 的顶部 `TopNav`（已删除）。与 `TopNav` 的差别不只是位置：
 *
 * | | `TopNav`（旧） | `SideNav`（本组件） |
 * |---|---|---|
 * | 入口 | 三个链接，按**任一所存令牌**的角色显示 | 同一条规则（见下），但收进左侧栏 |
 * | 个人信息 | 只有一句「已登录：…」 | 主体 / 角色 / 入口 / 账号 id |
 * | 权限状态 | 无 | 权限点列表（**来自 `/api/auth/me`**，不是前端副本） |
 * | 退出 | 无（要去登录页清） | 底部一个按钮 |
 *
 * ## 四条纪律
 *
 * ### ① 入口判据是「**任一已登录入口**的能力」，不是「当前路径那个入口」
 *
 * 变更 001 的存储层是**三把钥匙互不覆盖**（`shopsage.token.customer` / `.internal` / `.admin`）。
 * 若这里只读 `customer` 那把钥匙，管理员用 `/login` 登录后会落在 `/admin`、
 * 而侧边栏显示「去登录」—— 那是对既有行为的**回归**（旧 `TopNav` 用的是
 * `roles.some(...)`）。所以判据统一交给 `lib/auth.ts::visibleEntriesFor`（可单测的纯函数）。
 *
 * ### ② 这里的判断**只决定显示哪些链接**，不是安全边界
 *
 * 与 `lib/auth.ts` 的模块注释同源：客户把地址栏敲成 `/admin` 一样能打开那个页面 ——
 * 而那一页的每个请求都会被后端拒。前端分流的价值是「别让用户看见自己点不动的东西」。
 *
 * ### ③ 权限状态优先用 `/api/auth/me` 的**权威值**，失败才回退
 *
 * `lib/auth.ts` 的 `ROLE_PERMISSIONS` 自己标了「这是后端表的副本，不是真相」。
 * 而 `/me` 走的是 `require_principal`（`app/api/deps.py`），它会**查库校验账号状态
 * 与世代号** —— 所以它同时回答了两个问题：「我是谁」和「这条令牌还有效吗」。
 * 本地解析做不到后者。
 *
 * ⚠️ 回退时**必须在界面上注明是回退值**：否则一个「本地说是 admin、后端已经把他停用了」
 * 的账号会看到满权限的界面，然后每个请求都 401 —— 那是最难查的一类现场。
 *
 * ### ④ `ready` 之前不渲染结论
 *
 * 登录态在 `localStorage`（只有浏览器有），而本组件会在服务端预渲染一次。
 * 若那时就按「未登录」渲染，客户端 hydrate 之后会变成另一个样子（水合不一致警告）。
 * 这条沿用 `TopNav` 已定的口径。
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { getMe } from "@/lib/api";
import {
  browserStorage,
  entryForPath,
  entryLabel,
  entryPath,
  ROLE_PERMISSIONS,
  subPagesFor,
  visibleEntriesFor,
  type Entry,
} from "@/lib/auth";
import {
  clearSessionsForAccount,
  currentAccountId,
  readActiveSession,
  withFreshToken,
  type ActiveSession,
} from "@/lib/session";

/** 侧边栏里的一个入口（顺序即显示顺序，来自 `lib/auth.ts::ENTRIES`）。 */
const ITEM_META: Record<Entry, { label: string; hint: string }> = {
  customer: { label: "对客问答", hint: "聊天 + 流式 + 溯源" },
  internal: { label: "对内查询", hint: "报表 / 图表 / SQL 回显" },
  admin: { label: "管理台", hint: "开账号 / 停用 / 改角色" },
};

interface Identity {
  /** 主体标识串（`c2` / `e1`） */
  subject: string;
  role: string;
  /** 权限点列表 */
  permissions: string[];
  accountId: number | null;
  /** 这些值是**权威值**（来自 `/me`）还是**本地回退值** */
  source: "server" | "local";
  /** 回退原因（`source==="local"` 时给用户看） */
  fallbackReason: string;
}

/** 用本地令牌解析兜底（`/me` 失败时）。**必须**把 `source` 标成 `local`。 */
function localIdentity(session: ActiveSession, reason: string): Identity {
  return {
    subject: session.subject,
    role: session.role,
    permissions: [...(ROLE_PERMISSIONS[session.role] ?? [])],
    accountId: null,
    source: "local",
    fallbackReason: reason,
  };
}

export function SideNav() {
  const pathname = usePathname();
  const [sessions, setSessions] = useState<Record<string, ActiveSession | null> | null>(null);
  const [identity, setIdentity] = useState<Identity | null>(null);

  /** 当前路径对应的入口（身份面板显示「我现在是哪个入口的身份」）。 */
  const currentEntry = entryForPath(pathname ?? "/");

  const load = useCallback(async () => {
    const storage = browserStorage();
    const next: Record<string, ActiveSession | null> = {};
    for (const entry of ["customer", "internal", "admin"] as Entry[]) {
      next[entry] = readActiveSession(entry, storage);
    }
    setSessions(next);

    // 身份面板优先展示**当前路径那个入口**的令牌；没有则退到任一个已登录的入口
    // （客户从 `/` 进来时只有 customer 那把钥匙，这时两者是同一个）。
    const active =
      next[currentEntry] ?? next.customer ?? next.internal ?? next.admin ?? null;
    if (!active) {
      setIdentity(null);
      return;
    }
    try {
      // ⚠️ 走 `withFreshToken`（issue 003 的 P1-2）：身份自检也要能在令牌快过期时续期，
      //    否则用户看到的是「权限莫名回退到本地值」而不是一次正常的续期。
      //    入口用 `active.entry`（**实际持有令牌的那个**），不是 `currentEntry`
      //    （路径对应的入口）—— 两者在「从 / 进来但只有 internal 钥匙」时不同。
      const me = await withFreshToken(active.entry, storage, (fresh) => getMe(fresh));
      setIdentity({
        subject: String(me.subject ?? active.subject),
        role: String(me.role ?? active.role),
        permissions: Array.isArray(me.permissions) ? me.permissions.map((item) => String(item)) : [],
        accountId: typeof me.account_id === "number" ? me.account_id : null,
        source: "server",
        fallbackReason: "",
      });
    } catch (exc) {
      // 回退不是「静默兜底」：把原因带上，界面上会明确标出这是回退值。
      setIdentity(localIdentity(active, exc instanceof Error ? `${exc.name}: ${exc.message}` : String(exc)));
    }
  }, [currentEntry]);

  useEffect(() => {
    void load();
  }, [load]);

  const logout = () => {
    // ⚠️ 变更 003：**按账号清掉所有入口的钥匙**（不再是「只清当前入口」）。
    //
    // 变更 003 起登录会把同一枚令牌写进多把钥匙（`lib/session.ts::writeSessionPair`），
    // 所以只清当前入口会留下一条**隐形会话**：用户在 `/admin` 点了退出（看起来退成功了），
    // 而 `/` 与 `/internal` 的钥匙里还留着同一枚有效令牌 —— 会话其实还活着。
    // 那是一类「用户以为已经登出、实际没有」的安全问题。
    //
    // 判定「是本账号的」用令牌里的 `aid`；解不出来时退化成清全部（宁可比必要的多清一点）。
    const storage = browserStorage();
    const accountId = currentAccountId(storage);
    // 返回被清掉的入口列表（当前没消费它，但保留这个变量让「清了什么」在调试时可见）
    clearSessionsForAccount(accountId, storage);
    window.location.assign("/login");
  };

  // `ready` 之前不渲染结论（见模块注释 ④）
  if (!sessions) {
    return (
      <nav className="sidenav" aria-label="主导航">
        <Link className="sidenav-brand" href="/">
          <span className="brand-dot" aria-hidden="true" />
          ShopSage
        </Link>
        <div className="small muted" style={{ padding: "0 8px" }}>
          正在检查登录状态…
        </div>
      </nav>
    );
  }

  const roles = Object.values(sessions)
    .filter((item): item is ActiveSession => Boolean(item))
    .map((item) => item.role);
  const visible = visibleEntriesFor(roles);
  const activeSession = sessions[currentEntry] ?? sessions.customer ?? null;
  // 变更 003：子页导航（对客 → 我的订单；对内 → 退款审批）。
  // 判据仍然只是**显隐**：每一个子页的请求都会被后端按权限单独校验。
  const pages = subPagesFor(currentEntry, roles);

  return (
    <nav className="sidenav" aria-label="主导航">
      <Link className="sidenav-brand" href="/">
        <span className="brand-dot" aria-hidden="true" />
        ShopSage
        <span className="brand-sub">智能助手</span>
      </Link>

      <div className="sidenav-group">功能入口</div>
      {visible.map((entry) => {
        const meta = ITEM_META[entry];
        const path = entryPath(entry);
        return (
          <Link
            key={entry}
            className={`sidenav-link${pathname === path ? " active" : ""}`}
            href={path}
            title={meta.hint}
          >
            <span>{meta.label}</span>
            {pathname === path ? <span className="small">当前</span> : null}
          </Link>
        );
      })}
      {visible.length < 3 ? (
        <div className="small muted" style={{ padding: "6px 10px" }}>
          其余入口需要更高权限，未显示（真正的拦截在后端）。
        </div>
      ) : null}

      {/* ── 变更 003：当前入口下的子页 ──────────────────────────────────── */}
      {pages.length > 1 ? (
        <>
          <div className="sidenav-group">{entryLabel(currentEntry)}的子页</div>
          {pages.map((page) => (
            <Link
              key={page.path}
              className={`sidenav-link sub${pathname === page.path ? " active" : ""}`}
              href={page.path}
              title={page.hint}
            >
              <span>{page.label}</span>
              {pathname === page.path ? <span className="small">当前</span> : null}
            </Link>
          ))}
        </>
      ) : null}

      <div className="sidenav-foot">
        <div className="sidenav-group" style={{ padding: "0 8px 4px" }}>
          个人信息
        </div>
        {activeSession ? (
          <>
            <div className="small" style={{ padding: "0 8px" }}>
              主体 <span className="mono">{identity?.subject ?? activeSession.subject}</span>
            </div>
            <div className="small" style={{ padding: "0 8px" }}>
              角色 <span className="chip">{identity?.role ?? activeSession.role}</span>
            </div>
            <div className="small" style={{ padding: "0 8px" }}>
              入口 <span className="badge">{entryLabel(currentEntry)}</span>
            </div>
            {identity?.accountId !== null && identity?.accountId !== undefined ? (
              <div className="small" style={{ padding: "0 8px" }}>
                账号 id <span className="mono">{identity.accountId}</span>
              </div>
            ) : null}

            <div className="sidenav-group" style={{ padding: "8px 8px 4px" }}>
              权限状态
            </div>
            <div className="row" style={{ padding: "0 8px", gap: 4 }}>
              {identity && identity.permissions.length > 0 ? (
                identity.permissions.map((permission) => (
                  <span className="chip" key={permission}>
                    {permission}
                  </span>
                ))
              ) : (
                <span className="small muted">（没有读到权限点）</span>
              )}
            </div>
            {identity?.source === "local" ? (
              // ⚠️ 回退值必须显式标注（见模块注释 ③）：否则「本地说是 admin、
              // 后端已停用」的账号会看到满权限界面，然后每个请求都 401。
              <div className="small" style={{ padding: "4px 8px", color: "var(--warn)" }}>
                以上为本地回退值（未与后端核对）：{identity.fallbackReason}
              </div>
            ) : (
              <div className="small muted" style={{ padding: "4px 8px" }}>
                以上来自 <span className="mono">GET /api/auth/me</span>（服务端权威值）
              </div>
            )}

            <button className="btn ghost tiny" type="button" style={{ margin: "4px 8px" }} onClick={logout}>
              退出登录
            </button>
          </>
        ) : (
          <Link className="btn ghost tiny" href="/login" style={{ margin: "4px 8px" }}>
            去登录
          </Link>
        )}
      </div>
    </nav>
  );
}
