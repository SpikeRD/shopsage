"use client";

/**
 * ECharts 的**最小 React 包装**（只有几十行，所以不引 `echarts-for-react`）。
 *
 * 要处理的三件事（每件都对应一个真实的坑）：
 *
 * 1. **`echarts` 只能在浏览器里加载**：它是带 DOM 依赖的大包，直接 `import` 会进
 *    SSR 的 bundle（Next 会在服务端执行模块顶层代码）。所以用**动态 import** 在 effect 里加载；
 * 2. **实例必须显式 dispose**：不 dispose 的话，切页/重渲染会留下占着 canvas 的实例，
 *    表现是「来回切几次页面后内存一路涨」；
 * 3. **容器要有高度**：ECharts 拿不到高度就画不出来（宽高为 0），
 *    报错只是一句 `Can't get dom width or height` 的 warning —— 很容易被误判成「数据没来」。
 *    所以 `.chart-box` 在 CSS 里给了 `min-height`。
 *
 * ⚠️ 两个 effect 的分工是刻意的（第一版写成「一个 effect 里 init + setOption」，
 * 结果是**换数据不重画**：effect 的依赖是空的，第二份数据永远画不出来）：
 *
 * * 第一个 effect 只负责「init / dispose」，`[]` 依赖 —— 实例只建一次；
 * * 第二个 effect 只负责 `setOption`，依赖 `option` —— 数据变了就重画。
 */

import { useEffect, useRef } from "react";
import type { EChartsOption } from "@/lib/chart";

type EChartsInstance = {
  setOption: (option: EChartsOption, notMerge?: boolean) => void;
  resize: () => void;
  dispose: () => void;
};

type EChartsModule = { init: (element: HTMLElement, theme?: string | null) => EChartsInstance };

export function EChart({ option, height = 340 }: { option: EChartsOption; height?: number }) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsInstance | null>(null);
  /** 最近一次的 option：init 完成得比首次渲染晚，所以要把它补画上去 */
  const latestRef = useRef<EChartsOption>(option);
  latestRef.current = option;

  useEffect(() => {
    let disposed = false;
    let onResize: (() => void) | null = null;

    void (async () => {
      const mod = (await import("echarts")) as unknown as EChartsModule;
      if (disposed || !boxRef.current) return;
      const chart = mod.init(boxRef.current, null);
      chartRef.current = chart;
      // 补画：如果数据在 init 之前就到了，这一步是唯一能把它画出来的地方
      chart.setOption(latestRef.current, true);
      onResize = () => chart.resize();
      window.addEventListener("resize", onResize);
    })();

    return () => {
      disposed = true;
      if (onResize) window.removeEventListener("resize", onResize);
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    // 实例还没建好时什么都不做 —— 上面那次 `setOption(latestRef.current)` 会补上
    chartRef.current?.setOption(option, true);
  }, [option]);

  return <div className="chart-box" style={{ height }} ref={boxRef} data-testid="chart" />;
}
