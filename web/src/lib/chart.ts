/**
 * 图表适配层：**后端的 spec → 绘图库的 option**（`16.web.md` §5 第二条）。
 *
 * ## 为什么前端要有个适配层
 *
 * 后端 `app/tools/chart.py::build_chart_spec` 返回的是**结构化 spec**
 * （`{type, x, series, data, meta}`，与绘图库无关），前端把它翻成 ECharts 的 `option`。
 * 这条分工的意义：**换绘图库不改后端**。这里的 `toEChartsOption()` 是唯一
 * 「知道 ECharts 长什么样」的地方 —— 换 Recharts 只需要重写这一个函数，
 * `chart_spec` 的消费方（MCP 客户端、评测）一个都不用动。
 *
 * ## spec 里的三件事前端**不许自作主张**
 *
 * 1. **数据点已经截断过了**（`meta.truncated_points`）—— 图表不是数据导出通道，
 *    前端不许「顺手把剩下的画上」；
 * 2. **空值不补 0** —— 后端明确写了「不补」（NULL 与 0 是两件事）；ECharts 里
 *    用 `null` 表示缺测点，折线会断开，那才是诚实的画法；
 * 3. **`meta.warnings` 要显示出来**（`null_values` / `truncated_points` / 空数据）——
 *    这是后端刻意的「有声音」设计，前端把它吞掉等于让用户以为数据是全的。
 *
 * 本模块是**纯函数**：不碰 DOM、不 import ECharts 本体（只产出普通对象），
 * 所以能在 Node 测试里直接断言（`web/tests/chart.test.ts`）。
 */

import type { ChartSpec } from "./contracts.ts";

/** ECharts 的 option 是一个很大的联合类型；这里只用得着其中一小块，自己声明更清楚。 */
export interface EChartsOption {
  title?: { text: string; subtext?: string; left?: string };
  tooltip?: Record<string, unknown>;
  legend?: Record<string, unknown>;
  grid?: Record<string, unknown>;
  xAxis?: Record<string, unknown>;
  yAxis?: Record<string, unknown>;
  series: Record<string, unknown>[];
  dataset?: Record<string, unknown>;
}

/** 表格视图（`type === "table"` 或用户主动切表时用）。 */
export interface TableView {
  columns: string[];
  rows: (string | number | null)[][];
}

/** 把 spec 的 series 名字取出来（饼图/单系列时给 tooltip 用）。 */
function seriesNames(spec: ChartSpec): string[] {
  return spec.series.map((item) => item.name);
}

/** x 轴标签序列（`data` 里 x 列的值；可能不是字符串，统一成字符串）。 */
export function xValues(spec: ChartSpec): string[] {
  return spec.data.map((row) => {
    const value = row[spec.x.field];
    return value === null || value === undefined ? "" : String(value);
  });
}

/**
 * 数值序列（缺值保留 `null`）。
 *
 * ⚠️ 这里**不做 `Number()` 兜底**：spec 里已经是 number 或 null
 * （后端 `_to_number()` 处理过），再兜一次会把「后端给了个字符串」
 * 这种契约漂移静默吃掉。
 */
export function seriesValues(spec: ChartSpec, field: string): (number | null)[] {
  return spec.data.map((row) => {
    const value = row[field];
    return typeof value === "number" ? value : null;
  });
}

/** 表格视图：列 = x + 各系列，行按 spec 顺序（**不排序**：顺序是后端 sort_by 的结果）。 */
export function toTableView(spec: ChartSpec): TableView {
  const fields = [spec.x.field, ...seriesNames(spec)];
  return {
    columns: fields,
    rows: spec.data.map((row) => fields.map((field) => (row[field] ?? null) as string | number | null)),
  };
}

/** spec 的 `meta` 是不是有条件要提示（前端把它显示在图下方的小字里）。 */
export function chartWarnings(spec: ChartSpec): string[] {
  const warnings = [...(spec.meta?.warnings ?? [])];
  if (spec.meta?.truncated_points) {
    warnings.push(
      `后端只画了前 ${spec.meta.points} 个点（源数据 ${spec.meta.source_rows} 行）——图表不是数据导出通道。`,
    );
  }
  return warnings;
}

/**
 * 主入口：spec → ECharts option。
 *
 * `table` 类型在这里退化成柱状图之外的东西：ECharts 画表格很难看，
 * 前端用 `toTableView()` 走 HTML 表格（`ChartView` 组件据此分支）。
 * 所以这个函数对 `table` 抛错 —— **不要**静默画成柱状图（那会让「表格」这个
 * 类型的语义在两个地方不一样）。
 */
export function toEChartsOption(spec: ChartSpec): EChartsOption {
  if (spec.type === "table") {
    throw new Error("table 类型不走 ECharts：请用 toTableView() 渲染 HTML 表格");
  }
  const names = seriesNames(spec);
  const categories = xValues(spec);
  const base = {
    title: { text: spec.title, left: "left" },
    tooltip: { trigger: spec.type === "pie" ? "item" : "axis" },
    legend: { bottom: 0 },
    grid: { left: 48, right: 24, top: 48, bottom: 48 },
  };

  if (spec.type === "pie") {
    return {
      ...base,
      series: [
        {
          type: "pie",
          radius: "60%",
          name: names[0] ?? spec.x.field,
          data: spec.data.map((row) => ({
            name: String(row[spec.x.field] ?? ""),
            value: row[names[0] as string] ?? null,
          })),
        },
      ],
    };
  }

  if (spec.type === "scatter") {
    // 散点图的两个轴都是数值：x 列在这里是数值轴，不是类目轴
    const yField = names[0] ?? spec.x.field;
    return {
      ...base,
      xAxis: { type: "value", name: spec.x.field },
      yAxis: { type: "value", name: yField },
      series: names.map((name) => ({
        type: "scatter",
        name,
        data: spec.data.map((row) => [row[spec.x.field] ?? null, row[name] ?? null]),
      })),
    };
  }

  // bar / line：类目轴 + 多系列
  return {
    ...base,
    xAxis: {
      type: "category",
      name: spec.x.field,
      data: categories,
      // 类目多了以后标签挤成一团，转 30 度 + 截断是「还能读」的折中
      axisLabel: { rotate: categories.length > 8 ? 30 : 0, hideOverlap: true },
    },
    yAxis: { type: "value" },
    series: names.map((name) => ({
      type: spec.type,
      name,
      data: seriesValues(spec, name),
      // 折线图的缺测点不连线：`connectNulls: false` 是**默认值**，写出来是为了
      // 让「不补 0、不连线」这条口径在代码里看得见
      connectNulls: false,
      // 点多了不画点（否则 200 个点的柱状图会变成一团色块）
      showSymbol: spec.data.length <= 30,
    })),
  };
}

/** 数据量大不大（决定要不要默认折叠成表格：20 行以上的图在页面上很难读）。 */
export function isLargeDataset(spec: ChartSpec, rows = 20): boolean {
  return spec.data.length > rows;
}
