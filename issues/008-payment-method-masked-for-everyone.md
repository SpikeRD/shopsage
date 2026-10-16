# issue 008 · `payment_method` 对所有角色都被打码成 `***`（脱敏表选错了掩码函数）

| 项 | 值 |
|---|---|
| 发现时间 | 2026-10-16（变更 003 开发期） |
| 状态 | **未修**（不在变更 003 范围内，见下） |
| 影响面 | 所有展示 `payment_method` 的出口（客户订单页、对内查询、队列报表） |
| 判据 | `mask_pii([{"payment_method": "alipay"}], role=<任意角色>)` 的 `payment_method` 都是 `"***"` |

## 现象

客户订单页要求「订单列表 + 明细 + 支付方式」。实测发现**六个角色的
`payment_method` 全都是 `***`** —— 包括 `finance_auditor` 与 `admin`（财务级）。

```text
$ uv run python -c "from app.tools.orders import list_orders_for_customer; ..."
customer             payment_method=['***']
customer_service     payment_method=['***']
internal_employee    payment_method=['***']
manager              payment_method=['***']
finance_auditor      payment_method=['***']
admin                payment_method=['***']
```

## 根因

`app/security/masking.py::FIELD_RULES` 里 `payment_method` 登记的掩码函数是
`mask_bank_card`，等级是 `SENSITIVITY_FINANCIAL`（3）。

而 `payment_method` 的**真实取值**是 `alipay` / `wechat` / `card` / `cod`
（`app/db/models.PAYMENT_METHODS`）—— **一个数字都没有**。
`mask_bank_card` 对「位数不足 8」的输入走「整体打码」那条分支（那本身是对的，
它防的是「越打越长」），于是：

* 对清晰度 **< 3** 的角色：`level > ceiling` → 直接 `mask_any` → `***`（预期行为）；
* 对清晰度 **= 3** 的角色：`level >= SENSITIVITY_MASKED` → 调 `mask_bank_card` →
  没有数字 → 位数不足 → 返回 `***`（**非预期**）。

也就是说：**财务级角色也拿不到自己本该看到的字段**，
而这不是「策略保守」，是**给枚举值配了一个面向长数字串的掩码函数**。

## 为什么变更 003 不修它

变更 003 的决策 D1 明确写的是「金额与支付方式**走现有** masking，**不另写规则**」，
并把它记成了一处已识别的缺陷（变更单 §1.1 的 S12 与 §8 的 L2）。
顺手改脱敏规则表会**扩大本次变更的范围**（它影响所有出口，需要独立验收：
尤其是「改成什么才算对」——`alipay` 这种枚举值本就不该被打码，而「打码枚举值」
要么是纯枚举直出、要么是映射成「支付宝/微信」这类展示名，两者是不同的产品决定）。

## 建议的修法（择一，需独立变更）

1. **枚举值不打码**：把 `payment_method` 的掩码函数换成 `mask_any` 之外的恒等
   （即「只要清晰度够就原样返回」），并在等级上保留 3 —— 那么 <3 仍看不到；
2. **改成展示名映射**：`alipay → 支付宝`，并同样只对清晰度 ≥3 的角色可见；
3. 若产品上认为「支付方式连财务级也不该看到」，那就把等级提到「不可见」并
   **从 `FIELD_RULES` 里删掉**（现状是「登记了但谁也看不到」，最容易被误读成
   「已按等级脱敏」）。

## 相关

- 变更 003 单里的记录：[`docs/changes/003-refund-workflow-and-entry-permissions.md`](../docs/changes/003-refund-workflow-and-entry-permissions.md) §1.1 S12 / §8 L2
- 现有用例（**故意断言现状**，避免假绿）：
  `tests/api/test_orders_api.py::test_payment_method_is_masked_for_customer`
  与 `::test_masking_levels_actually_vary_by_role`（后者用 `name` 字段做
  「按等级裁剪」的反向对照，正是因为 `payment_method` 对照不出差别）。
