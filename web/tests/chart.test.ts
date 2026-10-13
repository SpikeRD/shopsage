/**
 * 图表适配层的单元测试（离线）。
 *
 * fixture 的形状**逐字抄** `app/tools/chart.py::build_chart_spec` 的产物
 * （`schema` / `type` / `title` / `x` / `series` / `data` / `meta`）。
 * 抄错的风险由另一条证据兜住：`live.test.ts` 里有一条用例拿**真后端返回的 spec**
 * 过一遍同一个适配函数（§7.4）。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { chartWarnings, seriesValues, toEChartsOption, toTableView, xValues } from "../src/lib/chart.ts";
import type { ChartSpec } from "../src/lib/contracts.ts";

function spec(overrides: Partial<ChartSpec> = {}): ChartSpec {
  return {
    schema: "shopsage.chart/v1",
    type: "bar",
    title: "订单金额 · 日期",
    x: { field: "date", label: "date" },
    series: [{ name: "amount", field: "amount", type: "bar" }],
    data: [
      { date: "2026-10-01", amount: 1200.5 },
      { date: "2026-10-02", amount: 980 },
      { date: "2026-10-03", amount: null },
    ],
    meta: {
      audit_id: "a1b2c3",
      caller: "agent:analysis",
      source_rows: 3,
      points: 3,
      truncated_points: false,
      null_values: 1,
      sorted_by: null,
      descending: false,
      warnings: ["1 个单元格是空值（NULL）：图上会缺这些点，未做 0 填充"],
    },
    ...overrides,
  };
}

test("柱状图：类目轴取 x 列，系列取声明列，缺值保留 null（不补 0）", () => {
  const option = toEChartsOption(spec());
  assert.deepEqual(xValues(spec()), ["2026-10-01", "2026-10-02", "2026-10-03"]);
  assert.deepEqual(seriesValues(spec(), "amount"), [1200.5, 980, null]);
  assert.equal((option.xAxis as { type: string }).type, "category");
  assert.equal(option.series.length, 1);
  assert.equal(option.series[0].type, "bar");
  assert.deepEqual(option.series[0].data, [1200.5, 980, null]);
  assert.equal(option.series[0].connectNulls, false, "缺测点不连线（后端明确不补 0）");
});

test("折线图：类型透传，点少时画点（showSymbol）", () => {
  const option = toEChartsOption(spec({ type: "line" }));
  assert.equal(option.series[0].type, "line");
  assert.equal(option.series[0].showSymbol, true);
});

test("多系列：一列一个 series，名字与顺序跟 spec 一致", () => {
  const option = toEChartsOption(
    spec({
      series: [
        { name: "paid", field: "paid", type: "bar" },
        { name: "refunded", field: "refunded", type: "bar" },
      ],
      data: [
        { date: "2026-10-01", paid: 100, refunded: 10 },
        { date: "2026-10-02", paid: 200, refunded: 20 },
      ],
    }),
  );
  assert.deepEqual(
    option.series.map((item) => item.name),
    ["paid", "refunded"],
  );
  assert.deepEqual(option.series[1].data, [10, 20]);
});

test("饼图：单系列，data 是 {name, value} 形状", () => {
  const option = toEChartsOption(
    spec({
      type: "pie",
      x: { field: "status", label: "status" },
      series: [{ name: "count", field: "count", type: "pie" }],
      data: [
        { status: "已签收", count: 12 },
        { status: "已退款", count: 3 },
      ],
    }),
  );
  assert.equal(option.series[0].type, "pie");
  assert.deepEqual(option.series[0].data, [
    { name: "已签收", value: 12 },
    { name: "已退款", value: 3 },
  ]);
});

test("散点图：两个轴都是数值轴，数据点成对", () => {
  const option = toEChartsOption(
    spec({
      type: "scatter",
      x: { field: "amount", label: "amount" },
      series: [{ name: "days", field: "days", type: "scatter" }],
      data: [{ amount: 100, days: 3 }],
    }),
  );
  assert.equal((option.xAxis as { type: string }).type, "value");
  assert.equal((option.yAxis as { type: string }).type, "value");
  assert.deepEqual(option.series[0].data, [[100, 3]]);
});

test("table 类型不画图：**抛错**而不是静默画成柱状图", () => {
  assert.throws(() => toEChartsOption(spec({ type: "table" })), /table 类型不走 ECharts/);
});

test("表格视图：列 = x + 各系列，行按 spec 顺序（不重排）", () => {
  const table = toTableView(spec());
  assert.deepEqual(table.columns, ["date", "amount"]);
  assert.deepEqual(table.rows, [
    ["2026-10-01", 1200.5],
    ["2026-10-02", 980],
    ["2026-10-03", null],
  ]);
});

test("缺列不崩：data 里没有该列时给 null（后端 `_check_field` 已经拦过，这里只做展示兜底）", () => {
  const option = toEChartsOption(
    spec({ data: [{ date: "2026-10-01", amount: 1 }, { date: "2026-10-02" } as never] }),
  );
  assert.deepEqual(option.series[0].data, [1, null]);
});

test("警告要带出来：后端的 warnings + 截断提示（前端不许吞掉）", () => {
  const truncated = spec({
    meta: {
      ...spec().meta,
      source_rows: 120,
      points: 50,
      truncated_points: true,
      warnings: [],
    },
  });
  const warnings = chartWarnings(truncated);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /前 50 个点/);
  assert.match(warnings[0], /源数据 120 行/);
});

test("空数据：**返回空图**而不是抛错（「没有数据」是数据状况，不是调用错误）", () => {
  const empty = spec({
    data: [],
    meta: { ...spec().meta, source_rows: 0, points: 0, null_values: 0, warnings: ["没有数据行：图表为空（这是数据状况，不是调用错误）"] },
  });
  const option = toEChartsOption(empty);
  assert.deepEqual(option.series[0].data, []);
  assert.match(chartWarnings(empty)[0], /没有数据行/);
});

test("类目多时标签旋转（可读性；不是业务规则）", () => {
  const many = spec({
    data: Array.from({ length: 12 }, (_, index) => ({ date: `2026-10-${index + 1}`, amount: index })),
  });
  const option = toEChartsOption(many);
  assert.equal((option.xAxis as { axisLabel: { rotate: number } }).axisLabel.rotate, 30);
});
