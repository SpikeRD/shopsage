"use client";

/**
 * 探活横幅（变更 002 / T4）——**挂在两个 layout 的顶部，每次打开/刷新自动跑一次**。
 *
 * ## 变更前是什么样
 *
 * 探活是登录页上的一个**手动按钮**（「探活 /health」）。后果：用户打开页面时如果后端
 * 没起，看到的是一片空白或后续操作的报错，**不会**在第一时间被告知「后端没连上」。
 *
 * ## 三条口径
 *
 * ### ① 自动的那一次在 `useEffect` 里，不在模块顶层
 *
 * `useEffect` 只在客户端跑。放到模块顶层会让 SSR/构建期去 `fetch`（构建机没有后端，
 * 会直接让 `next build` 失败）。这条与 `lib/health.ts` 的「纯函数 + 调用方负责时机」
 * 分工是配套的。
 *
 * ### ② 正常时**不显示任何东西**
 *
 * 横幅只在 `state !== "ok"` 时出现。一个常驻的「后端正常」提示，几天后就会被当成
 * 页面装饰忽略掉 —— 那时它出现在异常场景里也不再有人看。
 *
 * ### ③ 判据用 `lib/health.ts::judgeHealth`，**不看 `response.ok`**
 *
 * `/health` 恒 200（见 `lib/health.ts` 的模块注释）。Postgres 挂了也是 200，
 * 所以必须看 `body.status`。这个分支有单独用例（`tests/health.test.ts`）。
 *
 * ⚠️ `retry` 按钮是刻意保留的：自动探活只覆盖「打开/刷新」，后端是在页面**打开之后**
 * 才挂掉的话，用户需要一个不刷新就能重查的出口。
 */

import { useCallback, useEffect, useState } from "react";
import { getHealth } from "@/lib/api";
import { apiBase } from "@/lib/config";
import { probeHealth, type ProbeResult } from "@/lib/health";

/** 组件级状态的一行（横幅里列出非 ok 的组件）。 */
function ComponentLine({ name, status, detail }: { name: string; status: string; detail: string }) {
  return (
    <div className="small muted">
      <span className="mono">{name}</span>{" "}
      <span className={`badge ${status === "down" ? "danger" : "warn"}`}>{status}</span> {detail}
    </div>
  );
}

export function HealthBanner() {
  const [result, setResult] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);

  const probe = useCallback(async () => {
    setProbing(true);
    try {
      const next = await probeHealth(getHealth, { apiBase: apiBase() });
      setResult(next);
    } finally {
      setProbing(false);
    }
  }, []);

  // 页面每次打开/刷新各执行一次（空依赖数组 = 挂载时一次）。
  // ⚠️ StrictMode 下开发模式会跑两遍 —— 那是**故意的**，两次都是只读的 GET /health。
  useEffect(() => {
    void probe();
  }, [probe]);

  const judgement = result?.judgement ?? null;
  // 首次探活还没回来时不渲染：这一瞬间显示「后端没连上」会是假警报。
  if (!judgement || judgement.state === "ok") return null;

  const payload = result?.payload ?? null;
  const unreachable = judgement.state === "unreachable";

  return (
    <div className={`health-banner${judgement.state === "degraded" ? " warn" : ""}`} role="alert">
      <div className="health-banner-body">
        <strong>
          {unreachable ? "⚠ 后端未连接" : judgement.state === "down" ? "⚠ 后端依赖异常" : "⚠ 后端降级运行"}
        </strong>
        <div className="small" style={{ marginTop: 2 }}>
          {judgement.summary}
        </div>
        <div className="small muted" style={{ marginTop: 2 }}>
          后端地址：<span className="mono">{apiBase()}</span>
          {payload ? (
            <>
              {" "}
              · env=<span className="mono">{payload.env}</span> · version=
              <span className="mono">{payload.version}</span>
            </>
          ) : null}
        </div>
        {payload
          ? payload.components
              .filter((item) => item.status === "down" || item.status === "degraded")
              .map((item) => (
                <ComponentLine key={item.name} name={item.name} status={item.status} detail={item.detail} />
              ))
          : null}
        {judgement.detail ? <pre>{judgement.detail}</pre> : null}
      </div>
      <button className="btn ghost tiny" type="button" disabled={probing} onClick={() => void probe()}>
        {probing ? "探活中…" : "重新探活"}
      </button>
    </div>
  );
}
