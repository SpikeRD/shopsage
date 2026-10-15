import type { ReactNode } from "react";
import { HealthBanner } from "@/components/HealthBanner";

/**
 * 登录外壳（变更 002 / T6）—— **只有一张居中的卡片，没有侧边栏**。
 *
 * ## 为什么登录页要单独一套布局
 *
 * 需求原文是「登录界面和主界面分开」。这里用 Next 的**路由组** `(auth)` 落地：
 * 括号目录**不参与 URL**，所以这一页仍然是 `/login`（书签、runbook、live 用例的地址都不变）。
 *
 * 相比之下，「一个布局 + 按 `pathname` 隐藏侧边栏」的做法会让登录页的 HTML 里
 * **先存在侧边栏节点再藏掉** —— 首屏会闪一下。路由组在构建期就分开了。
 *
 * ## 探活横幅也挂在这里
 *
 * 需求是「页面每次打开或者刷新时就执行一次探活」。登录页正是**最需要**这条提示的地方：
 * 后端没起时，用户在这里点「登录」只会拿到一句 `Failed to fetch`，而横幅会在
 * 他点之前就说清楚「后端未连接」以及怎么查（见 `components/HealthBanner.tsx`）。
 */
export default function AuthLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <div className="auth-shell">
      <div className="auth-card">
        <HealthBanner />
        {children}
      </div>
    </div>
  );
}
