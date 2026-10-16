/**
 * 变更 003 的**前端纯函数**用例：订单/退款的契约解析 + 展示辅助。
 *
 * ## 为什么这些值得单独一组
 *
 * 它们都是「错了不会报错、只会显示错东西」的那一类：
 *   * 把 `needs_order_id` 渲染成「已提交」→ 客户以为退款申请成功了（实际没落库）；
 *   * 把 `payment_method` 的 `null` 与 `"***"` 混起来 → 界面分不清「没有」与「看不到」；
 *   * 幂等键在提交时生成而不是打开表单时生成 → 重复点会落两张单。
 *
 * 所以判据必须是**可断言的值**，而不是「跑起来看着对」。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError } from "../src/lib/api.ts";
import {
  orderStatusLabel,
  parseOrderList,
  parseRefundList,
  parseRefundOutcome,
  refundStatusLabel,
} from "../src/lib/contracts.ts";
import {
  describeApiFailure,
  formatTime,
  lineTotal,
  newRefundKey,
  refundBadgeClass,
} from "../src/lib/refund_ui.ts";

// ── 退款结果：三种形状必须分开 ───────────────────────────────────────────────

test("★退款结果：`submitted` 解析出单号与金额", () => {
  const outcome = parseRefundOutcome({
    status: "submitted",
    refund_id: 12,
    order_id: 1024,
    amount: "58.00",
    state: "requested",
    remaining_after: "0.00",
    idempotent_hit: true,
  });
  assert.equal(outcome?.status, "submitted");
  assert.equal(outcome?.refund_id, 12);
  assert.equal(outcome?.amount, "58.00");
  assert.equal(outcome?.idempotent_hit, true);
});

test("★退款结果：`needs_order_id` **没有**单号（没落库，不能显示成已提交）", () => {
  const outcome = parseRefundOutcome({
    status: "needs_order_id",
    candidates: [1024, 1025],
    hint: "让客户选",
  });
  assert.equal(outcome?.status, "needs_order_id");
  assert.equal(outcome?.refund_id, undefined, "没有落库就不该有单号");
  assert.deepEqual(outcome?.candidates, [1024, 1025]);
});

test("退款结果：候选订单号是字符串时也认（后端可能给 str）", () => {
  const outcome = parseRefundOutcome({ status: "needs_order_id", candidates: ["1024", "1025"] });
  assert.deepEqual(outcome?.candidates, [1024, 1025]);
});

test("★退款结果：认不出的 status 一律返回 null（宁可不出卡片，也不瞎猜）", () => {
  // 猜错的代价是「显示了一条错的业务事实」，比不显示严重得多
  assert.equal(parseRefundOutcome({ status: "future_state" }), null);
  assert.equal(parseRefundOutcome(null), null);
  assert.equal(parseRefundOutcome("submitted"), null);
  assert.equal(parseRefundOutcome([]), null);
});

test("退款结果：`refused` 保留 hint（那句话要原样给客户看）", () => {
  const outcome = parseRefundOutcome({ status: "refused", hint: "订单当前状态不支持退款" });
  assert.equal(outcome?.status, "refused");
  assert.equal(outcome?.hint, "订单当前状态不支持退款");
});

// ── 订单与退款列表的解析 ─────────────────────────────────────────────────────

test("★订单解析：`payment_method` 的 null 与 `***` 必须区分开", () => {
  const payload = parseOrderList({
    orders: [
      {
        order_id: 1,
        status: "delivered",
        total_amount: "100.00",
        payment_method: null,
        remaining_refundable: "100.00",
        refundable: true,
      },
      {
        order_id: 2,
        status: "shipped",
        total_amount: "50.00",
        payment_method: "***",
        remaining_refundable: "50.00",
        refundable: true,
      },
    ],
    total: 2,
    limit: 50,
  });
  assert.equal(payload.orders[0].payment_method, null, "null = 真的没有支付方式");
  assert.equal(payload.orders[1].payment_method, "***", "*** = 有但当前角色看不到");
  assert.equal(payload.orders[0].refundable, true);
});

test("订单解析：缺字段时给安全默认，不抛（缺 items/refunds 当成空数组）", () => {
  const payload = parseOrderList({ orders: [{ order_id: 7 }] });
  assert.equal(payload.orders.length, 1);
  assert.deepEqual(payload.orders[0].items, []);
  assert.deepEqual(payload.orders[0].refunds, []);
  assert.equal(payload.orders[0].total_amount, "0.00");
  assert.equal(payload.orders[0].refundable, false, "认不出来时必须当成「不可退」");
});

test("★退款列表解析：审批留痕字段齐全（谁批的/何时/意见/角色快照）", () => {
  const payload = parseRefundList({
    refunds: [
      {
        refund_id: 3,
        order_id: 9,
        user_id: 2,
        amount: "12.00",
        status: "approved",
        reason: "不想要了",
        decided_by: 2,
        decided_at: "2026-10-16T10:00:00+00:00",
        decision_note: "已核实",
        approver_role: "customer_service",
      },
    ],
    total: 1,
  });
  const row = payload.refunds[0];
  assert.equal(row.decided_by, 2);
  assert.equal(row.approver_role, "customer_service");
  assert.equal(row.decision_note, "已核实");
  assert.equal(row.reason, "不想要了", "客户的原话不能被审批意见覆盖");
});

test("状态标签：认得出来的给中文，认不出来的**原样显示**（不编一个词）", () => {
  assert.equal(refundStatusLabel("requested"), "待审批");
  assert.equal(refundStatusLabel("approved"), "已通过");
  assert.equal(refundStatusLabel("rejected"), "已驳回");
  assert.equal(refundStatusLabel("weird"), "weird");
  assert.equal(orderStatusLabel("delivered"), "已送达");
  assert.equal(orderStatusLabel("weird"), "weird");
});

// ── 展示辅助 ─────────────────────────────────────────────────────────────────

test("★徽章配色：待审批用 warn（它是唯一需要人去处理的状态）", () => {
  assert.equal(refundBadgeClass("requested"), "warn");
  assert.equal(refundBadgeClass("approved"), "ok");
  assert.equal(refundBadgeClass("refunded"), "ok");
  assert.equal(refundBadgeClass("rejected"), "danger");
});

test("小计：正常乘法；非法单价返回 `—`（不显示 NaN）", () => {
  assert.equal(lineTotal(3, "12.50"), "37.50");
  assert.equal(lineTotal(1, "0.10"), "0.10");
  assert.equal(lineTotal(2, ""), "—");
  assert.equal(lineTotal(2, "abc"), "—");
});

test("时间格式化：解析不出来的**原样返回**（不显示 Invalid Date）", () => {
  assert.equal(formatTime(null), "—");
  assert.equal(formatTime(""), "—");
  assert.equal(formatTime("不是时间"), "不是时间");
  assert.ok(formatTime("2026-10-16T10:00:00+00:00").length > 0);
});

test("★幂等键：字符集与长度合规，且两次调用不同", () => {
  const key = newRefundKey();
  // 后端 `validate_idempotency_key` 只允许 [A-Za-z0-9._:-] 且 ≤64
  assert.match(key, /^[A-Za-z0-9._:-]+$/);
  assert.ok(key.length <= 64, `键超长：${key.length}`);
  assert.ok(key.startsWith("web-refund-"));
  // 两次调用必须不同：同一个键会把「两次不同的申请意图」误判成同一次
  assert.notEqual(newRefundKey(), key);
});

// ── 错误话术 ─────────────────────────────────────────────────────────────────

test("★错误话术：四档分开（401/404/400/409），因为修法完全不同", () => {
  const unauthorized = describeApiFailure(
    new ApiError({ status: 401, code: "unauthorized", message: "令牌过期" }),
  );
  assert.match(unauthorized, /重新登录/);

  const notFound = describeApiFailure(
    new ApiError({ status: 404, code: "not_found_or_forbidden", message: "未找到该资源" }),
  );
  assert.match(notFound, /不属于你|没有这个权限/);

  const business = describeApiFailure(
    new ApiError({ status: 400, code: "refund_not_allowed", message: "退款金额超过剩余可退额度" }),
  );
  assert.match(business, /剩余可退额度/, "业务拒绝的原因要原样透传，不要改写成「操作失败」");

  const conflict = describeApiFailure(
    new ApiError({ status: 409, code: "refund_not_decidable", message: "当前状态是 approved" }),
  );
  assert.match(conflict, /刷新/, "409 的正确动作是刷新，不是再点一次");
});

test("★错误话术：没有状态码的失败走分诊话术（不是甩一句 Failed to fetch）", () => {
  // fetch 本身抛的 TypeError 没有 status，直接 String(exc) 对用户毫无用处
  const text = describeApiFailure(new TypeError("Failed to fetch"));
  assert.ok(text.length > 0);
  assert.doesNotMatch(text, /^TypeError/, "不该把原始异常名当成人话给用户看");
});
