# 模块 06 · tools（工具层）

## 目的

把"Agent 能对世界做的动作"收成一个受审计、受权限、受幂等保护的函数集合：**读**（SQL / 检索）与**写**（工单 / 退款）严格分开。

## 关键结论

1. **出口只有一个 `__all__`**（`app/tools/__init__.py`），外部（agents / api / mcp / queue）都从这里取，不直接 import 子模块。
2. **读写路径分离**（`docs/architecture.md:157` 的设计在代码里的落点）：
   - **读**：`execute_readonly_query()`（`app/tools/sql_executor.py:143`，只走只读 DSN）、`retrieve_docs()` / `retrieve_docs_async()`（`app/tools/retriever.py:153/195`）；
   - **写**：`create_ticket()`（`app/tools/ticket.py:71`）、`create_refund_request()` / `request_refund()` / `decide_refund()`（`app/tools/refund.py:378/479/618`）—— **全部是预定义 SQL，不经 LLM 生成**。
3. **退款工具的关键性质**（`app/tools/refund.py`，930 行，全仓最大的业务文件）：
   - 金额用 `Decimal`（`_to_money` :199），不用 float；
   - **幂等**：`uq_refunds_user_id_idempotency_key` 唯一约束 + `_insert_refund_once()`（:297）—— 同一 `(user_id, key)` 重复提交只落一行；
   - **额度**：`_remaining_refundable()`（:241）按「订单金额 − 已退金额」算剩余可退，超额直接拒；
   - 两个异常类型区分"业务上不许"（`RefundNotAllowed` :113）与"当前状态不可决策"（`RefundNotDecidable` :123）。
4. **表格行数上限两层**：校验层"声明"（`validate_sql` clamp）+ 执行层"兜底"（`clamp_bound_limit()`，`app/tools/sql_executor.py:111`）—— 因为参数化 `LIMIT :n` 只有第二层拦得住。
5. **审计**：`audit_tool_call()`（`app/tools/audit.py:227`）记录 caller / 时间 / 参数（敏感键脱敏）/ 结果行数；`caller` 约定值如 `agent:data`、`mcp:sql_query`。
6. **错误分类**：`ToolError` 派生出 `PermanentToolError` / `RetryableToolError` / `ToolContractError` / `ToolNotAvailableError`（`app/tools/errors.py:37-57`）；`translate_db_error()`（:129）按 SQLSTATE 判断瞬时故障。**安全异常原样抛**，只有数据库异常才翻译。
7. **图表工具返回 spec 不返回图片**：`build_chart_spec()`（`app/tools/chart.py:144`）产出 ECharts 的 `option` JSON，由前端 `web/src/lib/chart.ts:100 toEChartsOption` 渲染。

## 实际用法

```python
# app/tools/refund.py:479 —— 客户自助申请（变更 003 的唯一对客写路径）
def request_refund(*, user_id: int, order_id: int, reason: str, idempotency_key: str) -> RefundRef: ...

# app/tools/refund.py:618 —— 审批（只能员工，需 approve:refund）
def decide_refund(*, refund_id: int, approved: bool, approver_user_id: int, note: str = "") -> RefundView: ...

# app/tools/orders.py:167 / :218
def list_orders_for_customer(...) -> list[dict]     # 行级过滤 + 按角色脱敏 + 剩余可退额度
def get_order_view(...) -> dict
```

## 待办 / 已知边界

- `issues/008`：`payment_method` 经脱敏后对**所有角色**都是 `***`，所以订单页的"支付方式"列等于没有信息（`app/security/masking.py:136`）。
- `refund_rows_for_output()`（`app/tools/refund.py:866`）是"给输出用的视图"，与"给决策用的视图"分开 —— 改展示口径时只动前者。
- ⚠️ 各工具的**完整签名**未全部列出（27 个导出名），需要时执行 `uv run python -c "import inspect, app.tools as t; [print(n, inspect.signature(getattr(t,n))) for n in t.__all__]"`（`app/tools/__init__.py` 里已写了这条命令）。

## 来源

- `docs/modules/6.tools.md`（123 行）、`docs/runbook/6.tools-runbook.md`（828 行）
- `docs/changes/003-refund-workflow-and-entry-permissions.md`
- 代码：`app/tools/*.py`；测试：`tests/test_tools_*.py`
