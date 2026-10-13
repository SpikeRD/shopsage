"""MCP 验收与上手脚本（`docs/runbook/15.mcp-runbook.md` §6 的实测输出出自这里）。

五个场景，每个都能**单独跑**（默认全跑）：

| 场景 | 它回答的问题 | 需要什么 |
|---|---|---|
| `discover` | 外部客户端能发现什么？（工具清单 / 描述 / 注解 / 输入 schema） | 零依赖 |
| `guard` | 越权与写语句真的会被拦吗？（D1 在这条入口上是否仍然成立） | 零依赖（纯静态守卫） |
| `offline` | 整条协议链路能跑通吗？（进程内真 `Client` + 假执行器） | 零依赖 |
| `query` | 真库上一条只读查询长什么样？（含 Decimal → 字符串的出口形状） | 真 PostgreSQL |
| `retrieve` | 真检索的命中与引用长什么样？ | 真库 + pgvector + 本地 embedding |
| `stdio` | 换一个进程、走真的 stdio 传输，客户端还能连上吗？ | 零依赖（起子进程） |

⚠️ **`--policy internal` 时预期会变**（本步实测踩到的坑，见 runbook §10.1 坑 11）：
`guard` 场景里「缺行级过滤 / 字面量过滤」两条**按设计放行**，它们会打印 `➖` 而不是 `✅`。
其余场景照旧全绿（内部策略只是不再强制行级过滤，守卫与工具能力不变）。

为什么要有 `stdio` 这一场（不是重复劳动）
------------------------------------------

进程内 `Client(server)` 证明的是「装配正确」，它**绕过了传输层**。
而真实的 MCP 客户端（Cursor / Claude 桌面端）是**起一个子进程、走 stdin/stdout 的 JSON-RPC**。
两者的差别是真实存在的：stdio 模式下 `stdout` 是协议通道，任何一句 `print`
都会让客户端读不懂服务端的消息（本步把启动日志写去 stderr 就是为它，见 §6.5）。
这个场景用**真的子进程**跑一次握手 + `tools/list`，把那类事故挡在演示阶段。

退出码（与第 14 步评测同一套口径）：0 全部通过 / 1 有场景未达标 / 2 环境问题。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from typing import Any, Final

from app.mcp import sync_calls
from app.mcp.server import build_server, describe_tools
from app.mcp.settings import EXPOSED_TOOLS, McpSettings, SERVER_NAME, SERVER_VERSION
from app.mcp.sync_calls import dumps
from app.security.sql_guard import SQLSecurityError, validate_sql
from mcp.server.mcpserver.exceptions import ToolError


def _use_utf8_output() -> None:
    """把本进程的 stdout/stderr 切到 UTF-8。

    ⚠️ 这不是「顺手加一行」，而是**本脚本在 Windows 上能不能跑的前提**（实测报错）：

        UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'

    Windows 上 Python 默认按控制台的活动代码页编码标准输出（中文机器是 cp936/GBK），
    而本脚本的输出里有 ✅ / ❌ / ➖ 这些 GBK 编不出来的字符 —— 于是**第一行明细就崩**，
    更糟的是崩溃发生在 `print` 里、被场景级 `except` 接住，最后表现为
    「所有场景都报 ❌ 且输出乱码」，看起来像功能坏了。

    `reconfigure` 在 Python 3.7+ 可用；`errors="replace"` 是兜底（宁可显示成 `?`，
    也不要让一次编码问题把整场演示炸掉）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - 极老的解释器或已被替换的流
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 已分离的流
            continue


#: 只读查询的演示 SQL（带行级过滤绑定；对客策略下这是**唯一**能通过的形态）
DEMO_SQL: Final[str] = (
    "SELECT order_id, status, total_amount FROM orders "
    "WHERE user_id = :user_id ORDER BY order_id LIMIT 3"
)

#: **两种策略下都必须被拦**的形态（静态守卫，与身份无关）。
#: ⚠️ 这一组是 `scenario_guard` 的硬断言：任何一条没被拦住就说明守卫被绕过了。
GUARD_DEMO: Final[tuple[tuple[str, str], ...]] = (
    ("写语句", "DELETE FROM orders"),
    ("多语句", "SELECT 1; DROP TABLE orders"),
    ("无 WHERE 全表", "SELECT order_id FROM orders"),
    ("白名单外的表", "SELECT order_id FROM secret_table WHERE user_id = :user_id"),
)

#: **只有 `customer` 策略才拦**的形态（本步新增的行级过滤那一层）。
#: ⚠️ 它们在 `internal` 策略下**应该被放行** —— 那正是「内部策略是放宽」的具体含义
#: （runbook §4.3 的风险清单）。实测踩到的形态：第一版把这两条和上面那四条写在一起，
#: 于是 `--policy internal` 必然报「没拦住」（runbook §10.1 坑 11）——
#: **演示脚本也必须随策略切换预期**，否则它会把「设计如此」报成「故障」。
CUSTOMER_ONLY_DEMO: Final[tuple[tuple[str, str, dict[str, Any]], ...]] = (
    ("缺行级过滤", "SELECT order_id FROM orders WHERE status = :s LIMIT 5", {"user_id": 1, "s": "paid"}),
    ("字面量过滤", "SELECT order_id FROM orders WHERE user_id = 1 LIMIT 5", {"user_id": 1}),
)


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _block(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 74 - len(title)))


def scenario_discover(settings: McpSettings) -> bool:
    """工具清单 + 注解 + 参数（客户端看到的**就是**这些）。"""
    _section("场景 1：客户端能发现什么（零依赖）")
    print(f"服务：{SERVER_NAME} {SERVER_VERSION}")
    print(
        f"策略：{settings.policy} | 身份：user_id={settings.actor_user_id} "
        f"role={settings.actor_role}"
    )
    print(
        f"上限：top_k ≤ {settings.max_top_k} | rows ≤ {settings.max_rows} | "
        f"points ≤ {settings.max_chart_points}"
    )
    print()
    print(f"{'工具':<20}{'性质':<10}{'参数'}")
    for item in describe_tools(settings):
        nature = "只读" if item["read_only"] else "写"
        if item["idempotent"]:
            nature += "+幂等"
        print(f"{item['name']:<20}{nature:<10}{', '.join(item['parameters'])}")
    expected = set(EXPOSED_TOOLS)
    found = {item["name"] for item in describe_tools(settings)}
    ok = found == expected
    print()
    print(f"对外清单对账：{'✅ 一致' if ok else '❌ 不一致'}（{sorted(found)}）")
    return ok


def scenario_guard(settings: McpSettings) -> bool:
    """静态守卫 + 行级过滤在这条入口上照旧生效（不需要库）。

    三种拒绝的**异常类型不同**，演示必须都接住（实测漏接过一个）：

    * `SQLSecurityError`（带 `rule`）：`security.sql_guard` 的静态拦截；
    * `ToolError`：本层自己的拒绝（行级过滤那几条），规则名写在消息开头的方括号里。

    ⚠️ 预期**随策略切换**（实测踩到的坑，见 runbook §10.1 坑 11）：

    * `customer`：4 条守卫 + 2 条行级过滤，**全部**必须被拦（硬断言）；
    * `internal`：只有 4 条守卫是硬断言；行级过滤那 2 条**应当被放行**，
      它们这一场的价值变成「看见放宽的具体含义」（runbook §4.3 的风险清单）。

    这一条之所以值得写进脚本而不是只写进文档：**演示脚本的预期也是一份断言**，
    它写死了 `customer` 的预期却接受 `--policy internal`，就会把「设计如此」报成「故障」。
    """
    _section("场景 2：越权与写语句会被拦吗（零依赖，纯静态校验）")
    print(f"策略：{settings.policy}（{_policy_note(settings)}）")

    all_blocked = True
    for label, sql in GUARD_DEMO:
        try:
            sync_calls.sql_query_sync(sql, {"user_id": 1, "s": "paid"}, None, settings=settings)
            print(f"  ❌ {label}：**没拦住** —— {sql}")
            all_blocked = False
        except SQLSecurityError as exc:
            print(f"  ✅ {label}：BLOCK[{exc.rule}]（静态守卫）")
        except ToolError as exc:
            print(f"  ✅ {label}：BLOCK[{_rule_of(exc)}]（MCP 入口的边界校验）")

    for label, sql, params in CUSTOMER_ONLY_DEMO:
        if settings.is_customer_scoped:
            try:
                sync_calls.sql_query_sync(sql, params, None, settings=settings)
                print(f"  ❌ {label}：**没拦住** —— {sql}")
                all_blocked = False
            except (SQLSecurityError, ToolError) as exc:
                print(f"  ✅ {label}：BLOCK[{_rule_of(exc)}]（MCP 入口的边界校验）")
        else:
            # 内部策略下这条**应该**放行：这是放宽，不是漏检。
            # 只做静态校验（不执行），所以不需要库。
            validate_sql(sql, max_rows=settings.max_rows)
            print(f"  ➖ {label}：按设计放行（`internal` 策略不强制行级过滤，见 runbook §4.3）")

    # 正例只做**静态**校验（不执行）：本场景要能在没有库的机器上跑
    validate_sql(DEMO_SQL, max_rows=settings.max_rows)
    if settings.is_customer_scoped:
        sync_calls.assert_customer_row_filter(DEMO_SQL)
        print("  ✅ 正例：带 `WHERE user_id = :user_id` 的查询通过静态校验（未执行）")
    else:
        print("  ✅ 正例：带 `WHERE user_id = :user_id` 的查询通过静态校验（未执行）")
    return all_blocked


def _policy_note(settings: McpSettings) -> str:
    """一句话说清当前策略意味着什么（演示输出要能自己解释自己）。"""
    return (
        "强制行级过滤（缺过滤/字面量/跨用户一律拒绝）"
        if settings.is_customer_scoped
        else "不强制行级过滤（放宽：任何能连上本服务的客户端都能查全站，见 runbook §4.3）"
    )


def _rule_of(exc: BaseException) -> str:
    """从异常里取规则名：`SQLSecurityError.rule` 是字段，`ToolError` 的写在消息开头。"""
    rule = getattr(exc, "rule", None)
    if isinstance(rule, str) and rule:
        return rule
    return str(exc).split("]")[0].lstrip("[")


async def scenario_offline(settings: McpSettings) -> bool:
    """整条协议链路（进程内真 Client + 假执行器）：握手 → 发现 → 调用 → 错误结果。"""
    _section("场景 3：协议链路能跑通吗（零依赖，进程内真 Client）")
    from mcp import Client

    from app.mcp import sync_calls

    calls: list[dict[str, Any]] = []

    def fake_executor(sql, params, *, max_rows, caller=None):
        calls.append({"sql": sql, "params": dict(params or {}), "max_rows": max_rows, "caller": caller})
        return (
            [{"order_id": 3, "status": "pending", "total_amount": __import__("decimal").Decimal("2745.98")}],
            {
                "audit_id": "tc-demo",
                "row_count": 1,
                "truncated": False,
                "columns": ["order_id", "status", "total_amount"],
                "tables": ["orders"],
                "declared_limit": max_rows,
                "limit_clamped": False,
                "bound_limit_clamped": False,
                "rows_cap": max_rows,
                "duration_ms": 3,
            },
        )

    original = sync_calls.execute_readonly_query
    sync_calls.execute_readonly_query = fake_executor
    try:
        async with Client(build_server(settings)) as client:
            tools = (await client.list_tools()).tools
            print(f"握手得到 {len(tools)} 个工具：{[t.name for t in tools]}")
            ok = await client.call_tool("sql_query", {"sql": DEMO_SQL, "params": {"user_id": 1}})
            print("✅ 正常调用返回（注意金额是**字符串**，不是 float）：")
            print("   " + dumps(json.loads(_text(ok))).replace("\n", "\n   ")[:400])
            bad = await client.call_tool("sql_query", {"sql": "DELETE FROM orders"})
            print(f"✅ 写语句 → is_error={bad.is_error}：{_text(bad).splitlines()[0][:90]}")
    finally:
        sync_calls.execute_readonly_query = original
    print(f"   假执行器收到的 max_rows（已被夹到服务端上限）：{calls[0]['max_rows']}")
    print(f"   薄壳传给工具层的 caller：{calls[0]['caller']!r}（None = 由入口 caller_override 盖章）")
    return True


def scenario_query(settings: McpSettings) -> bool:
    """真库上的一条只读查询（依赖不可达时跳过并说清怎么修）。

    ⚠️ 用的是**本服务的身份**（`settings.actor_user_id`），而不是随便挑一个客户：
    对客策略下绑定值必须等于身份，挑别人只会撞上 `mcp_row_filter_mismatch` ——
    那是**正确行为**，但会让演示看起来像坏了（第一版就是这么写的，runbook §10.1 坑 6）。
    """
    _section("场景 4：真库上的一条只读查询")
    from sqlalchemy import select

    from app.db.models import Order
    from app.db.session import SessionLocal

    actor = settings.actor_user_id
    if actor is None:
        print("⚠️ 跳过：内部策略下需要显式指定客户（本演示脚本只演示对客身份）")
        return True
    try:
        with SessionLocal() as db:
            orders = db.execute(
                select(Order.order_id).where(Order.user_id == actor).limit(3)
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001 - 演示脚本：依赖不可达就跳过并说清怎么修
        print(f"⚠️ 跳过：连不上 PostgreSQL（{type(exc).__name__}）。检查 .env 的 DATABASE_URL host")
        return True
    if not orders:
        print(f"⚠️ 跳过：客户 user_id={actor} 没有订单（先跑 `uv run python -m scripts.seed`）")
        return True

    result = sync_calls.sql_query_sync(DEMO_SQL, {"user_id": actor}, None, settings=settings)
    print(f"客户 user_id={actor} 的前 3 笔订单：")
    print(dumps(result))
    print()
    print("两个读法：① `total_amount` 是字符串（精度不在这条边界上丢）；")
    print("          ② `meta.audit_id` 能直接去日志里 grep 到这一次调用。")
    return True


def scenario_retrieve(settings: McpSettings) -> bool:
    """真检索（向量库空 / 模型不可用时跳过并说明）。"""
    _section("场景 5：真检索的命中与引用")
    try:
        result = sync_calls.retrieve_knowledge_sync("无理由退货的运费谁承担", 3, settings=settings)
    except Exception as exc:  # noqa: BLE001 - 演示脚本：依赖不可达就跳过
        print(f"⚠️ 跳过：检索后端不可用（{type(exc).__name__}: {str(exc)[:80]}）")
        return True
    if not result["results"]:
        print("⚠️ 向量库是空的：先跑 `uv run python -m scripts.load_knowledge_base`")
        print("   （跑过全量回归之后必然为空：test_migration_round_trip 会 downgrade base）")
        return True
    for hit in result["results"]:
        print(f"[{hit['score']:.4f}] {hit['citation']}")
        print(f"          {hit['text'][:70]}…")
    print()
    print(f"返回 {result['meta']['returned']} 条（全部带 source：无溯源的在工具层就被丢弃）")
    return True


def scenario_stdio(settings: McpSettings, timeout: float = 30.0) -> bool:
    """真的起一个子进程，走 stdio JSON-RPC 握手 + tools/list（`runbook §6.5` 的证据）。"""
    _section("场景 6：换一个进程、走真 stdio 传输")
    process = subprocess.Popen(
        [sys.executable, "-m", "app.mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(_repo_root()),
        env=_child_env(settings),
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        handshake = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-demo", "version": "0.1"},
            },
        }
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(handshake) + "\n")
        process.stdin.flush()
        response = _read_json_line(process.stdout, timeout)
        info = (response or {}).get("result", {}).get("serverInfo", {})
        print(f"initialize → {info}")

        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n"
        )
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        process.stdin.flush()
        listing = _read_json_line(process.stdout, timeout, expect_id=2)
        names = [tool["name"] for tool in ((listing or {}).get("result") or {}).get("tools", [])]
        print(f"tools/list → {names}")
        ok = info.get("name") == SERVER_NAME and set(names) == set(EXPOSED_TOOLS)
        print(f"{'✅' if ok else '❌'} stdio 传输可用（stdout 只跑协议，日志走 stderr）")
        return ok
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - 演示脚本的兜底
            process.kill()


def _read_json_line(stream, timeout: float, *, expect_id: int | None = None) -> dict[str, Any] | None:
    """从子进程 stdout 读一行 JSON-RPC 响应（跳过通知行）。

    用**独立线程 + 队列**而不是直接 `readline`：直接读会一直阻塞到子进程吐出东西，
    而一个挂住的子进程会让演示脚本永远停在那里（排障时最讨厌的形态）。
    """
    result: list[str | None] = [None]

    def reader() -> None:
        while True:
            line = stream.readline()
            if not line:
                result[0] = None
                return
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 的行（例如被误打印到 stdout 的日志）跳过
            if expect_id is not None and payload.get("id") != expect_id:
                continue
            result[0] = line
            return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if result[0] is None:
        return None
    return json.loads(result[0])


def _child_env(settings: McpSettings) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["MCP_POLICY"] = settings.policy
    env["MCP_ACTOR_ROLE"] = settings.actor_role
    if settings.actor_user_id is not None:
        env["MCP_ACTOR_USER_ID"] = str(settings.actor_user_id)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def _text(result: Any) -> str:
    return "\n".join(getattr(item, "text", "") for item in (result.content or ()))


SCENARIOS = {
    "discover": ("工具清单与注解", scenario_discover),
    "guard": ("越权/写语句拦截", scenario_guard),
    "offline": ("协议链路（进程内）", scenario_offline),
    "query": ("真库只读查询", scenario_query),
    "retrieve": ("真检索", scenario_retrieve),
    "stdio": ("真 stdio 传输", scenario_stdio),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.mcp_demo",
        description="MCP 层验收脚本（默认跑全部场景）",
    )
    parser.add_argument(
        "--scenario",
        choices=(*SCENARIOS, "all"),
        default="all",
        help="只跑某一个场景（默认全部）",
    )
    parser.add_argument("--actor-user-id", type=int, default=1, help="演示用的身份 user_id（默认 1）")
    parser.add_argument(
        "--policy", choices=("customer", "internal"), default="customer", help="演示用的策略"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果摘要（给 CI 消费）")
    args = parser.parse_args(argv)

    # ⚠️ 必须在**任何 print 之前**切 UTF-8：本脚本的输出含 ✅/❌/➖，
    #    在 cp936 的 Windows 上不切换就会以 UnicodeEncodeError 崩溃（见 `_use_utf8_output`）。
    _use_utf8_output()

    settings = McpSettings(policy=args.policy, actor_user_id=args.actor_user_id)
    problems = settings.validate()
    if problems:
        print("[MCP 演示无法启动] " + "；".join(problems))
        return 2

    selected = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results: dict[str, bool] = {}
    for name in selected:
        label, fn = SCENARIOS[name]
        try:
            outcome = fn(settings)
            if hasattr(outcome, "__await__"):  # async 场景
                import asyncio

                outcome = asyncio.run(outcome)
            results[name] = bool(outcome)
        except Exception as exc:  # noqa: BLE001 - 演示脚本：任何场景崩了都要继续跑完
            print(f"  ❌ 场景 {name}（{label}）崩了：{type(exc).__name__}: {exc}")
            results[name] = False

    _section("汇总")
    for name in selected:
        print(f"  {'✅' if results[name] else '❌'} {name:<12}{SCENARIOS[name][0]}")
    summary = {"ok": all(results.values()), "scenarios": results}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    start = time.perf_counter()
    code = main()
    print(f"\n耗时 {time.perf_counter() - start:.1f}s，退出码 {code}")
    raise SystemExit(code)
