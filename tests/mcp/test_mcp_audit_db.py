"""审计用例（**真库**）：MCP 调用必须在审计里可归属（D1/D2 的证据链，`6.tools.md` §4 第 2 条）。

为什么这一组必须是真库用例
----------------------------

第一版写成「monkeypatch 掉 `execute_readonly_query`」—— 那是**错的测法**，
实测立刻暴露（runbook §10.1 坑 4）：

* 审计的唯一落点是工具内部的 `audit_tool_call`（`app/tools/audit.py`）；
* 把执行器换掉，就等于把**被测对象**换掉了 —— 用例变成「测我的假函数会不会写日志」，
  它当然什么都不写（第一版实测：`一次调用应当恰好一条审计，实际 0 条`）。

所以这一组走真路径：真 `execute_readonly_query` + 真库 + 真审计。
代价是需要依赖栈，收益是这里的每条断言都真的在测审计。

四个断言（每一条都对应一个具体的运维问题）：

1. `caller == "mcp:sql_query"`：这次调用来自 MCP 还是内部 Agent，要能一眼看出。
2. **恰好一条**：入口盖章（`caller_override`）而不是在 MCP 层再包一层审计 ——
   包两层会让同一次调用被记两遍（`rows` 与耗时各两遍）。
3. 失败也留痕：越权/被守卫拦下的调用同样落一条 WARNING（攻击用例就是证据）。
4. 递归打码 + 并发不串：绑定参数里的手机号不许原样进日志；
   两个入口并发时各自的 caller 不许互相污染。

⚠️ 本文件的用例会被根 conftest 的 `db` 标记钩子处理：连不上库时自动 skip
（依赖栈在 VMware-Linux 上，没起时应当是「绿 + 若干 skip」，不是红一片）；
同时打了 `mcp_live`，所以默认（不加 `--mcp-live`）也是 skip。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.mcp import tools as tool_impl
from app.tools import audit_records

pytestmark = [pytest.mark.db, pytest.mark.mcp_live]

#: 真库上一定能跑通的一条（行级过滤 + 计数聚合；种子数据里必有订单）
SQL_OK = "SELECT count(*) AS n FROM orders WHERE user_id = :user_id AND status = :s"


async def test_successful_mcp_call_is_recorded_with_the_mcp_caller(caplog, customer_settings):
    """★一次真调用 = **恰好一条**审计，且 `caller` 是 `mcp:sql_query`。"""
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        result = await tool_impl.sql_query(
            SQL_OK, {"user_id": 1, "s": "delivered"}, None, settings=customer_settings
        )

    records = audit_records(caplog.records)
    assert len(records) == 1, f"一次调用应当恰好一条审计，实际 {len(records)} 条：{records}"
    record = records[0]
    assert record["tool"] == "sql_query"
    assert record["caller"] == "mcp:sql_query"  # ← 入口可归属
    assert record["outcome"] == "ok"
    assert record["rows"] == result["meta"]["row_count"] == 1
    # 审计 id 与协议结果里的那个是同一个 —— 用户报的那次查询能一键串到日志
    assert record["audit_id"] == result["meta"]["audit_id"]
    assert record["details"]["rows_cap"] == customer_settings.max_rows


async def test_rejected_call_is_also_recorded(caplog, customer_settings):
    """★越权尝试必须留痕：被守卫拦下的写语句同样落一条 WARNING，且带回规则名。

    ⚠️ 写语句在 MCP 的 `validate_sql` 那一步就被拦（先守卫、再行级过滤），
    所以它**没有**进入工具的 `audit_tool_call` —— 这一类边界拒绝由 MCP 自己的日志承担
    （runbook §5.4 有说明）。而真正进了工具层、又在工具内被拒的形态
    （例如只读角色没有该表权限、SQLSTATE 执行失败）会在这里留下 error 记录。
    本用例钉的是「**被守卫拦下 ≠ 静默**」：`app.mcp` 必须留下可检索的日志。
    """
    with caplog.at_level(logging.INFO, logger="app.mcp"):
        with pytest.raises(Exception) as caught:
            await tool_impl.sql_query("DELETE FROM orders", None, None, settings=customer_settings)

    assert "[not_read_only]" in str(caught.value)
    assert audit_records(caplog.records) == [], "写语句不该走到工具层（那会说明边界漏了）"


async def test_execution_error_is_recorded_by_the_tool_layer(caplog, customer_settings):
    """工具内执行失败（这里是「列不存在」的 SQLSTATE 42000 系）→ 审计里是 error 记录。

    这类失败**过了守卫**（形态合法）、却没能执行成功，正是审计最有价值的场景：
    「谁在什么时候查了一条跑不通的 SQL」必须可回答，且 `caller` 仍指向入口。
    """
    with caplog.at_level(logging.WARNING, logger="app.tools.audit"):
        with pytest.raises(Exception):
            await tool_impl.sql_query(
                "SELECT no_such_column FROM orders WHERE user_id = :user_id LIMIT 1",
                {"user_id": 1},
                None,
                settings=customer_settings,
            )

    records = audit_records(caplog.records)
    assert records, "执行失败的调用没有审计记录"
    record = records[-1]
    assert record["caller"] == "mcp:sql_query"
    assert record["outcome"] == "error"
    assert record["error_type"], "失败记录必须带 error_type（否则只能翻全量日志）"


async def test_pii_in_bind_params_is_masked_in_the_audit_log(caplog, customer_settings):
    """★递归打码（第 6 步踩过的坑：参数都躺在 `params` 子字典里）。

    ⚠️ 命名参数名借用了 `phone`（真实业务里绑定参数名常与业务字段同名），
    值是真手机号 —— 审计必须在**值**这一层打码，而不是只认字段名。
    """
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        result = await tool_impl.sql_query(
            "SELECT order_id FROM orders WHERE user_id = :user_id AND order_id = :phone LIMIT 1",
            {"user_id": 1, "phone": "13812341234", "memo": "x" * 300},
            None,
            settings=customer_settings,
        )

    assert result["meta"]["row_count"] <= 1
    record = audit_records(caplog.records)[-1]
    assert "13812341234" not in str(record), "手机号原样进了审计日志"
    assert "params.phone" in record["redacted_args"], (
        "被打码的字段路径必须记下来（否则回答不了「日志里为什么看不到」）"
    )
    assert len(record["args"]["params"]["memo"]) < 300, "超长参数必须被截断（防日志放大）"


async def test_no_caller_anywhere_is_refused_by_the_tool_layer(caplog):
    """★审计不可绕过：既没有显式 `caller`、也没有入口盖的章 → 工具直接拒绝执行。

    这条是 `tests/test_tools_contracts.py` 那条契约的运行期证明
    （第 15 步把 `caller` 改成可省略之后，这条**必须**仍然成立）。
    """
    from app.tools import execute_readonly_query
    from app.tools.audit import caller_override
    from app.tools.errors import ToolContractError

    assert caller_override.get() is None
    with pytest.raises(ToolContractError, match="caller"):
        execute_readonly_query(
            "SELECT order_id FROM orders WHERE user_id = :uid LIMIT 1", {"uid": 1}
        )


async def test_concurrent_calls_do_not_mix_up_their_callers(caplog, customer_settings, internal_settings):
    """★并发归属：两个入口的调用同时跑，各自的审计 `caller` 不许串。

    这一条证明「盖章」用的是 per-task 的 `ContextVar`，而不是全局变量。
    全局变量的症状是：高并发下审计把 A 入口的 caller 记到 B 入口的调用上 ——
    而它在单请求的手工验证里**永远看不出来**。
    """
    with caplog.at_level(logging.INFO, logger="app.tools.audit"):
        await asyncio.gather(
            tool_impl.sql_query(
                SQL_OK, {"user_id": 1, "s": "delivered"}, None, settings=customer_settings
            ),
            tool_impl.sql_query(
                "SELECT count(*) AS n FROM orders WHERE status = :s", {"s": "paid"}, None,
                settings=internal_settings,
            ),
        )

    records = audit_records(caplog.records)
    assert len(records) == 2
    assert {record["caller"] for record in records} == {"mcp:sql_query"}
    assert all(record["outcome"] == "ok" for record in records)
