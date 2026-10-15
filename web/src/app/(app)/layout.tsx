import type { ReactNode } from "react";
import Link from "next/link";
import { HealthBanner } from "@/components/HealthBanner";
import { SideNav } from "@/components/SideNav";

/**
 * 主界面外壳（变更 002 / T6）：**左侧边栏 + 右侧内容区**。
 *
 * ## 为什么是路由组
 *
 * `(app)` 是 Next 的**路由组**语法：括号目录**不参与 URL**，所以
 * `(app)/page.tsx` 仍是 `/`、`(app)/admin/page.tsx` 仍是 `/admin`。
 * 这样就能在不改任何 URL 的前提下，让主界面与登录页（`(auth)` 组）拥有**两套外壳**。
 *
 * ## 侧边栏是客户端组件，内容区是服务端组件
 *
 * 登录态在 `localStorage`（只有浏览器有），而本布局是服务端组件 —— 在这里直接读会
 * 渲染出「未登录」快照并与客户端的水合结果打架。所以侧边栏那部分必须交给
 * `"use client"` 的 `<SideNav/>`（见其模块注释 ④）。
 *
 * ## 探活横幅挂在**顶部**
 *
 * 放在 `<SideNav/>` 与内容区之间，而不是页面正文里：正文再长也不会把它滚出视野，
 * 而「后端没连上」正是那种「必须先看见」的信息。
 */
export default function AppLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <div className="app-shell">
      <SideNav />
      <div className="shell-main">
        <HealthBanner />
        {children}
        <footer className="footer">
          <span>
            演示层：所有权限判断在后端（对客走行级过滤、对内走 RBAC、管理台走 admin 权限点），
            前端只负责展示与分流。
          </span>
          <span>
            {" "}
            · <Link href="/login">切换身份 / 登录</Link>
          </span>
        </footer>
      </div>
    </div>
  );
}
