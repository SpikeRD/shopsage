import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ShopSage · 智能助手",
  description: "电商企业智能助手：对客问答 / 对内数据查询 / 账号管理台",
};

/**
 * 根布局（变更 002 起**只剩 HTML 骨架**）。
 *
 * ## 变更 002 从这里删掉了什么，为什么
 *
 * | 删掉的 | 原来为什么有 | 为什么可以删 |
 * |---|---|---|
 * | `<Script id="shopsage-theme">`（`beforeInteractive`） | 把 `localStorage` 里的主题偏好在首帧前写进 `<html data-theme>`，否则会闪一下浅色 | 主题现在**只有一套浅色**（见 `globals.css` 的模块注释）：没有偏好可读，也就没有闪烁问题 |
 * | `<header className="topbar">` + `<TopNav/>` | 顶部导航 | 入口改由**左侧边栏**承担（`(app)/layout.tsx` 的 `<SideNav/>`）；登录页根本不该有导航 |
 *
 * ## 为什么两套外壳不在这里
 *
 * 登录页与主界面要长得完全不同（前者无侧边栏、居中一张卡片）。如果在这个根布局里
 * 按 `pathname` 判断「要不要画侧边栏」，登录页的 HTML 里会**先有侧边栏节点再藏掉** ——
 * 首屏会闪。所以外壳交给两个**路由组**（`(auth)` / `(app)`）在构建期分开，
 * 本文件只负责 `<html lang="zh-CN">` 与全局样式。
 *
 * ⚠️ `<html lang="zh-CN">` 保持不变：界面是中文的，标错语言会让浏览器
 * （以及屏幕阅读器）用错的分词与朗读规则。
 *
 * ⚠️ 仍然**不用 `next/font/google`**：它会去 `fonts.googleapis.com` 拉字体，
 * 而本项目的构建/演示环境**不保证外网可达**（实测：构建期拉不到会让 `next build`
 * 直接失败）。字体用系统字体栈（`globals.css` 的 `--font-sans`）。
 */
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
