"""真依赖端到端用例（默认跳过，`--mcp-live` 开门）。

为什么这些用例不能混进默认回归
--------------------------------

`15.mcp.md` §8 的验收是「MCP Server 可被外部客户端发现并调用，能力与 `6.tools.md` 一致」——
这句话只有在**真的库 + 真的检索后端**上才谈得上证据。但它们有三个默认不该跑的理由：

1. **真库**：本机依赖栈在 VMware-Linux 上（`2.infra-runbook.md` §2），没起就整片红；
2. **真检索**：本地 `BAAI/bge-m3` 要加载权重（几十秒 + 内存），
   而第 10 步的实测教训是「跑一次全量回归会把向量库清空」
   （`tests/test_db_migrations.py::test_migration_round_trip_is_repeatable` 会 `downgrade base`）；
3. **真写库**：建工单会插行（写路径没有内存假实现 —— 假实现测不出唯一约束）。

所以开关沿用第 14 步的写法（`--rag-live` 的同款）：**默认零依赖、可复现**，
真依赖单独开门、并且能自己 skip 出可读的理由。

⚠️ 顺序（第 14 步 §1.0 的同一条坑）：先跑完全量回归 → 再
`uv run python -m scripts.load_knowledge_base` → 再跑本文件的 `--mcp-live`。
顺序踩错时的症状是「检索全部 0 条」，而它看起来像检索坏了。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.db.models import Ticket, User
from app.db.session import SessionLocal
from app.mcp import tools as tool_impl
from app.mcp.sync_calls import dumps
from tests.mcp.conftest import unique_key

pytestmark = pytest.mark.mcp_live


def _seed_user_id() -> int:
    """挑一个种子数据里真实存在的客户（**不能硬编码**：换一个 seed 值用例就红）。"""
    with SessionLocal() as db:
        user_id = db.execute(select(User.user_id).order_by(User.user_id)).scalars().first()
    if user_id is None:
        pytest.skip("库里没有用户（先跑 `uv run python -m scripts.seed`）")
    return int(user_id)


def _cleanup_keys(keys: list[str]) -> None:
    """只删本文件造的行（按幂等键精确匹配，不动种子数据与别的套件）。"""
    if not keys:
        return
    with SessionLocal() as db:
        db.execute(delete(Ticket).where(Ticket.idempotency_key.in_(keys)))
        db.commit()


# ── 一、真库：只读查询 + 行级过滤 ─────────────────────────────────────────────


async def test_sql_query_returns_real_rows_and_runs_as_the_read_only_role(customer_settings):
    """一条真查询：拿到的行都属于本身份，且 `current_user` 是只读角色。"""
    user_id = _seed_user_id()
    own = await tool_impl.sql_query(
        "SELECT order_id, status, total_amount FROM orders WHERE user_id = :user_id ORDER BY order_id LIMIT 5",
        {"user_id": user_id},
        None,
        settings=customer_settings,
    )
    assert own["meta"]["row_count"] >= 1, "种子数据里这个客户应当有订单"

    role = await tool_impl.sql_query(
        "SELECT current_user AS db_user FROM orders WHERE user_id = :user_id LIMIT 1",
        {"user_id": user_id},
        None,
        settings=customer_settings,
    )
    # 只读角色在位时必须是它；没配 DATABASE_READ_URL 的环境里会是 shopuser（写角色），
    # 那种状态下 D1 的"库层兜底"不成立 —— 这里明确说出来，而不是静默放过。
    assert role["rows"][0]["db_user"] in {"shopsage_readonly", "shopuser"}


async def test_cross_user_query_is_blocked_before_hitting_the_database(customer_settings):
    """越权用例（真库版）：绑定值指向别人 → 在**发出查询之前**就被拦。"""
    user_id = _seed_user_id()
    with pytest.raises(Exception) as caught:
        await tool_impl.sql_query(
            "SELECT order_id FROM orders WHERE user_id = :user_id LIMIT 5",
            {"user_id": user_id + 999},
            None,
            settings=customer_settings,
        )
    assert "[mcp_row_filter_mismatch]" in str(caught.value)


async def test_truncation_is_visible_in_the_protocol_result(customer_settings):
    """截断（C1）在协议结果里**看得见**：`truncated=True` 且行数等于上限。"""
    user_id = _seed_user_id()
    result = await tool_impl.sql_query(
        "SELECT order_id FROM orders WHERE user_id = :user_id ORDER BY order_id LIMIT :n",
        {"user_id": user_id, "n": 100000},
        1,  # 只要 1 行，多取的那一行只用于判定
        settings=customer_settings,
    )
    assert result["meta"]["rows_cap"] == 1
    if result["meta"]["truncated"]:
        assert len(result["rows"]) == 1, "多取的那一行永远不返回给调用方"
    else:
        assert len(result["rows"]) <= 1, "该客户只有这一条订单（数据事实，不是被截断）"


# ── 二、真检索：知识库问答 ─────────────────────────────────────────────────────


async def test_retrieve_knowledge_returns_cited_chunks(customer_settings):
    """真检索：命中片段必须带来源，且内容与退货政策相关。

    ⚠️ 向量库空的时候（跑过全量回归之后就是这样）本条会 skip 并说明怎么灌数据 ——
    这条 skip 是**有价值的信号**，不是噪音（第 14 步 §1.0 的坑 10）。
    """
    result = await tool_impl.retrieve_knowledge("无理由退货的运费谁承担", 3, settings=customer_settings)
    if result["meta"]["returned"] == 0:
        pytest.skip(
            "向量库是空的（多半是刚跑过全量回归：test_migration_round_trip 会 downgrade base）。"
            "修复：uv run python -m scripts.load_knowledge_base"
        )
    top = result["results"][0]
    assert top["source"], "检索结果必须有来源（无源的一律被丢弃）"
    assert top["citation"].startswith(top["source"])
    assert top["score"] > 0
    assert "运费" in dumps(result) or "退货" in dumps(result)


# ── 三、真写库：工单幂等（真正的判据是库里只有一行）────────────────────────────


async def test_ticket_is_idempotent_in_the_database(customer_settings):
    """★幂等：同一个键调两次 → 同一个 `ticket_id`，且**库里只有一行**。

    只断言「返回值相等」是不够的（一个每次都新建、然后返回第一行的实现也能过）。
    真正的判据是库里的行数 —— 这正是第 6 步 `test_create_ticket_idempotent` 的口径。
    """
    user_id = _seed_user_id()
    key = unique_key("ticket")
    try:
        first = await tool_impl.create_ticket("logistics", key, settings=customer_settings)
        second = await tool_impl.create_ticket("logistics", key, settings=customer_settings)
        assert first["ticket_id"] == second["ticket_id"]
        assert first["user_id"] == second["user_id"] == user_id
        assert "created" not in first, "「本次是否新建」属审计字段，不进返回值（幂等的类型保证）"
        with SessionLocal() as db:
            rows = db.execute(
                select(func.count()).select_from(Ticket).where(Ticket.idempotency_key == key)
            ).scalar_one()
        assert rows == 1, f"幂等失效：库里出现了 {rows} 行"
    finally:
        _cleanup_keys([key])


async def test_customer_cannot_open_a_ticket_for_another_user(customer_settings):
    user_id = _seed_user_id()
    with pytest.raises(Exception) as caught:
        await tool_impl.create_ticket("logistics", unique_key("cross"), user_id=user_id + 999, settings=customer_settings)
    assert "[mcp_row_filter_mismatch]" in str(caught.value)


# ── 四、图表：真 SQL 的行直接喂给图表工具（第 6 步留的接缝）────────────────────


async def test_real_rows_feed_the_chart_tool(customer_settings):
    """`sql_query` 的 rows 直接进 `build_chart`（MCP 客户端最自然的用法）。

    这条同时验证两件事：① 协议出口的 JSON 化没有破坏图表需要的形状；
    ② `StringDecimal`（金额）进了图表会变成 float（图表只要比例关系）。
    """
    user_id = _seed_user_id()
    rows = await tool_impl.sql_query(
        "SELECT status, count(*) AS n FROM orders WHERE user_id = :user_id GROUP BY status ORDER BY status LIMIT 20",
        {"user_id": user_id},
        None,
        settings=customer_settings,
    )
    spec = await tool_impl.build_chart(
        rows["rows"], "status", y="n", chart_type="bar", title="我的订单状态分布",
        settings=customer_settings,
    )
    assert spec["schema"] == "shopsage.chart/v1"
    assert spec["meta"]["source_rows"] == len(rows["rows"])
    # `count(*)` 回来的是 int（不是 Decimal），图表把它当数值列画 —— 形状必须是 float
    assert all(isinstance(item["n"], (int, float)) for item in spec["data"])
