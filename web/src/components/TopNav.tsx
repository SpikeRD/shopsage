"use client";

/**
 * 顶部导航（变更 001 / T17）：**按当前登录态渲染入口**，而不是永远显示三个链接。
 *
 * ## 为什么需要单独一个客户端组件
 *
 * `app/layout.tsx` 是**服务端组件**，而登录态在 `localStorage`（只有浏览器有）。
 * 在 layout 里读它会让构建期报错或渲染出「未登录」快照（然后 hydration 打架）。
 * 所以把一个 `"use client"` 的小组件挂进 layout —— 它在客户端读存储，
 * 首次渲染统一显示「未登录」的姿态（`ready` 之前不渲染结论），避免水合不一致。
 *
 * ## 一条纪律（与 `lib/auth.ts` 的模块注释同源）
 *
 * 这里的判断**只决定「显示哪些链接」**，不是安全边界。
 * 客户把地址栏敲成 `/admin` 一样能打开那个页面 —— 而那一页的每个请求都会被后端拒（404）。
 * 前端分流的价值是「别让用户看见自己点不动的东西」，不是「拦住他」。
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { canUseAdmin, canUseInternal, entryLabel, type Entry } from "@/lib/auth";
import { readActiveSession, type ActiveSession } from "@/lib/session";
import { currentStorage } from "@/lib/session";

/** 入口 → 该入口对应的会话来自哪个存储 key（三把钥匙互不覆盖，见 `lib/session.ts`）。 */
const NAV_ENTRIES: Entry[] = ["customer", "internal", "admin"];

export function TopNav() {
  const [sessions, setSessions] = useState<Record<string, ActiveSession | null> | null>(null);

  useEffect(() => {
    const storage = currentStorage();
    const next: Record<string, ActiveSession | null> = {};
    for (const entry of NAV_ENTRIES) next[entry] = readActiveSession(entry, storage);
    setSessions(next);
  }, []);

  // `ready` 之前不渲染结论：服务端渲染时读不到 localStorage，
  // 若这时就按「未登录」渲染，客户端 hydrate 之后会变成另一个样子（水合不一致警告）。
  if (!sessions) {
    return (
      <nav className="topnav">
        <Link href="/login">登录</Link>
      </nav>
    );
  }

  const customer = sessions.customer;
  const internal = sessions.internal;
  const admin = sessions.admin;
  // 任一入口的角色能读全站订单/是管理员 → 显示对应的入口链接。
  // ⚠️ 判据用**角色**（来自各自令牌），不是「最后登录的是谁」—— 三把钥匙独立。
  const roles = [customer?.role, internal?.role, admin?.role].filter(Boolean) as string[];
  const showInternal = roles.some((role) => canUseInternal(role));
  const showAdmin = roles.some((role) => canUseAdmin(role));

  return (
    <nav className="topnav">
      <Link href="/">对客问答</Link>
      {showInternal ? <Link href="/internal">对内查询</Link> : null}
      {showAdmin ? <Link href="/admin">管理台</Link> : null}
      <Link href="/login">{roles.length ? "登录 / 切换身份" : "登录"}</Link>
      {roles.length ? (
        <span className="small muted" title="当前已登录的入口">
          （已登录：{NAV_ENTRIES.filter((entry) => sessions[entry]).map(entryLabel).join(" · ")}）
        </span>
      ) : null}
    </nav>
  );
}
