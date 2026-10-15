import type { Metadata } from "next";
import Script from "next/script";
import Link from "next/link";
import "./globals.css";
import { TopNav } from "@/components/TopNav";

export const metadata: Metadata = {
  title: "ShopSage · 智能助手",
  description: "电商企业智能助手：对客问答 / 对内数据查询 / 账号管理台",
};

/**
 * 根布局。
 *
 * 两个刻意的做法：
 *
 * 1. **不用 `next/font/google`**：它会去 `fonts.googleapis.com` 拉字体，
 *    而本项目的构建/演示环境**不保证外网可达**（本机实测：构建期拉不到会让
 *    `next build` 直接失败）。字体在这里不是设计需求，是风险 ——
 *    用系统字体栈换掉（见 `globals.css` 的 `--font-sans`）。
 * 2. **`<html lang="zh-CN">`**：界面是中文的，标错语言会让浏览器
 *    （以及屏幕阅读器）用错的分词与朗读规则。
 * 3. **导航是一个客户端组件**（`<TopNav />`，变更 001 / T17）：
 *    登录态在 `localStorage`，而本布局是**服务端组件** —— 在这里直接读会渲染出
 *    「未登录」快照并与客户端的水合结果打架。导航要按角色显示不同入口，
 *    所以那部分必须交给客户端组件（见其模块注释）。
 */
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <Script id="shopsage-theme" strategy="beforeInteractive">
          {`try{var t=localStorage.getItem("shopsage.theme");if(t==="light"||t==="dark"){document.documentElement.dataset.theme=t;}}catch(e){}`}
        </Script>
        <header className="topbar">
          <Link className="brand" href="/">
            <span className="brand-dot" aria-hidden="true" />
            ShopSage
            <span className="brand-sub">智能助手</span>
          </Link>
          <TopNav />
        </header>
        <main className="shell">{children}</main>
        <footer className="footer">
          <span>
            演示层：所有权限判断在后端（对客走行级过滤、对内走 RBAC、管理台走 admin 权限点），
            前端只负责展示与分流。
          </span>
        </footer>
      </body>
    </html>
  );
}
