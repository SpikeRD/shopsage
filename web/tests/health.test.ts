/**
 * 探活判据的单元测试（变更 002 / T11）——**离线，不连后端**。
 *
 * ## 这组用例存在的唯一理由
 *
 * 后端 `GET /health` **恒返回 200**，依赖故障写在 body 的 `status` 里
 * （`app/api/health.py` 的刻意设计：存活探针拿到非 2xx 会重启进程，
 * 而依赖不通时进程本身是健康的）。
 *
 * 所以「只看 `response.ok`」是一个**会静默出错**的判据：Postgres 挂了也是 200，
 * 页面会显示「后端正常」。本文件里最关键的一条是
 * `★ HTTP 200 但 status="down" 必须判成异常` —— 只测「fetch 成功」的用例集
 * 会**漏掉整个分支**。
 *
 * ## 四态为什么不能合并
 *
 * `unreachable`（请求没被读到）与 `down`（后端活着但依赖不可达）的修法完全不同：
 * 前者查进程/跨源，后者查依赖栈。混成一句「后端异常」会把人引到错误的方向。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { HealthPayload } from "../src/lib/api.ts";
import { judgeHealth, probeHealth, unreachableJudgement } from "../src/lib/health.ts";

/** 造一个形状真实的 `/health` 响应（字段与 `app/api/schemas.py::HealthResponse` 对齐）。 */
function healthPayload(
  status: string,
  components: { name: string; status: string; detail?: string; latency_ms?: number | null }[] = [],
): HealthPayload {
  return {
    status,
    service: "shopsage-api",
    env: "dev",
    version: "0.1.0",
    components: components.map((item) => ({
      name: item.name,
      status: item.status,
      detail: item.detail ?? "",
      latency_ms: item.latency_ms ?? null,
    })),
  };
}

test("★ HTTP 200 但 status=\"down\" 必须判成异常（/health 恒 200，只看 response.ok 会漏判）", () => {
  // 这条就是那个 bug 的防回归：真实场景是 Postgres 不可达 —— HTTP 明明是 200。
  const judgement = judgeHealth(
    healthPayload("down", [
      { name: "postgres", status: "down", detail: "10.0.0.5:5432 不可达", latency_ms: 500 },
      { name: "redis", status: "ok", detail: "可连接", latency_ms: 2 },
    ]),
  );
  assert.equal(judgement.state, "down");
  assert.equal(judgement.reported, "down");
  assert.equal(judgement.problems.length, 1, "只该把 postgres 算成问题（redis 是 ok）");
  assert.equal(judgement.problems[0].name, "postgres");
  assert.match(judgement.detail, /postgres=down/);
  assert.match(judgement.detail, /不可达/);
});

test("★ status=\"ok\" 才是正常；且 skipped 组件不算问题（否则健康的开发环境天天报警）", () => {
  const judgement = judgeHealth(
    healthPayload("ok", [
      { name: "rate_limit", status: "ok", detail: "限流后端 = redis" },
      { name: "observability", status: "skipped", detail: "没配 OTEL_ENDPOINT" },
      { name: "unconfigured", status: "skipped", detail: "未配置连接串" },
    ]),
  );
  assert.equal(judgement.state, "ok");
  assert.deepEqual(judgement.problems, [], "skipped 不是问题（它是「没启用」，不是「坏了」）");
  assert.equal(judgement.detail, "");
});

test("status=\"degraded\" 判成降级（不是 down，也不是 ok）", () => {
  const judgement = judgeHealth(
    healthPayload("degraded", [
      { name: "redis", status: "down", detail: "6379 不可达", latency_ms: 500 },
      { name: "cache", status: "degraded", detail: "回落进程内存" },
    ]),
  );
  assert.equal(judgement.state, "degraded");
  assert.equal(judgement.problems.length, 2, "down 与 degraded 都算问题");
  assert.match(judgement.detail, /redis=down/);
  assert.match(judgement.detail, /cache=degraded/);
});

test("认不出来的 status **不当成 ok**（宁可多报一次警，也不静默显示正常）", () => {
  const judgement = judgeHealth(healthPayload("something_new"));
  assert.equal(judgement.state, "degraded");
  assert.match(judgement.summary, /无法识别/);
});

test("payload 为 null（没读到 body）判成 unreachable，绝不判成 ok", () => {
  const judgement = judgeHealth(null);
  assert.equal(judgement.state, "unreachable");
  assert.equal(judgement.reported, "");
});

test("★ fetch 抛异常 → unreachable，且细节里带「先别怀疑令牌」那套分诊", () => {
  const judgement = unreachableJudgement(new TypeError("Failed to fetch"), "http://127.0.0.1:8000");
  assert.equal(judgement.state, "unreachable");
  // 复用 lib/diagnostics.ts 的话术：它把「后端没起 / 地址错 / 跨源被拦」区分开了
  assert.match(judgement.detail, /探活失败/);
  assert.match(judgement.detail, /先别怀疑令牌/);
  assert.match(judgement.detail, /http:\/\/127\.0\.0\.1:8000/);
});

test("probeHealth：成功路径把 payload 与结论一起给出来（横幅要显示 components）", async () => {
  const result = await probeHealth(async () => healthPayload("ok"), { apiBase: "http://x" });
  assert.equal(result.judgement.state, "ok");
  assert.ok(result.payload, "成功时必须带原始 payload");
  assert.equal(result.payload?.service, "shopsage-api");
});

test("probeHealth：失败路径 payload 为 null、结论是 unreachable（不抛异常给调用方）", async () => {
  const result = await probeHealth(
    async () => {
      throw new TypeError("Failed to fetch");
    },
    { apiBase: "http://127.0.0.1:8999" },
  );
  assert.equal(result.judgement.state, "unreachable");
  assert.equal(result.payload, null);
  assert.match(result.judgement.detail, /127\.0\.0\.1:8999/);
});

test("probeHealth：200 + status=down 这条**端到端判据**（最容易漏的一条组合）", async () => {
  // 模拟真实的 /health：HTTP 层是 200（`getHealth` 不抛），问题在 body 里
  const result = await probeHealth(
    async () =>
      healthPayload("down", [{ name: "postgres", status: "down", detail: "不可达", latency_ms: 500 }]),
    { apiBase: "http://x" },
  );
  assert.equal(result.judgement.state, "down");
  assert.notEqual(result.judgement.state, "ok");
  assert.ok(result.payload, "拿到了 body，所以 payload 不该是 null");
});

test("组件状态里的 latency 会被带进 detail（慢与断是两种隐患，不能只看 up/down）", () => {
  const judgement = judgeHealth(
    healthPayload("degraded", [{ name: "postgres", status: "degraded", detail: "连上了但慢", latency_ms: 412.5 }]),
  );
  assert.match(judgement.detail, /412\.5ms/);
});
