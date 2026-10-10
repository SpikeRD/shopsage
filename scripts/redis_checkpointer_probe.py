"""Redis checkpointer 探针（第 8 步的**待验证项**：一跑就知道差什么）。

背景：`docs/modules/8.graph.md` §4 第 2 条要求 checkpoint 能落到 **Postgres/Redis**。
Postgres 那条已在仓库里验证（`tests/graph/test_graph_checkpoint.py::test_checkpoint_survives_a_new_graph_and_connection[postgres]`）；
Redis 那条在开发机上**跑不了** —— `langgraph-checkpoint-redis` 用 `redisvl` 建索引，
需要 **RediSearch + RedisJSON** 两个模块，而 `infra/docker-compose.dev.yml` 里的
`redis:7-alpine` 都没有。本脚本把「差什么、怎么补、补完怎么验」变成一条命令。

用法（在仓库根执行）：

    uv run python -m scripts.redis_checkpointer_probe            # 用 .env 里的 REDIS_URL
    uv run python -m scripts.redis_checkpointer_probe --url redis://192.168.167.130:6380/0

它做**三步检查**，每步失败都给出可照做的下一步：

| 步骤 | 检查什么 | 失败含义 |
|---|---|---|
| ① 连通性 | `PING` + 版本 | 地址/端口/防火墙不对（先去查这个，别急着怀疑模块） |
| ② 能力 | `FT._LIST`（RediSearch）、`JSON.SET`（RedisJSON） | 镜像不对 —— 换 `redis/redis-stack-server` 即可 |
| ③ 端到端 | 真开一次 saver：`setup()` → 写一个 thread → **换新连接读回** | API 不兼容 / 索引建不起来 / 序列化问题 |

退出码：0 = 全部通过；1 = 某一步失败（报文里带原因与修法）。

⚠️ 与 `scripts/graph_replay_demo.py` 的分工：那个演示**图的机制**（重放/短路/幂等/隔离），
本脚本只回答一个问题 —— **Redis 这个后端到底能不能用**。混在一起会让
「图有问题」与「Redis 没装模块」这两种故障看起来一样。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/redis_checkpointer_probe.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph import (  # noqa: E402
    CheckpointUnavailable,
    GraphRuntime,
    compile_graph,
    ensure_compatible_event_loop,
    graph_config,
    open_checkpointer,
)
from app.graph.nodes import NODE_GUARDRAIL_INPUT  # noqa: E402
from app.graph.state import NODE_ROUTER  # noqa: E402
from tests.graph_helpers import AgentFakeLLM, customer_state  # noqa: E402

PROBE_THREAD = "probe-redis-checkpointer"


def _line(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 66 - len(title)))


async def _step1_connectivity(url: str) -> str:
    """① 连通性：PING + 版本 + 已有 checkpoint 键数量。"""
    _line("① 连通性")
    import redis.asyncio as aioredis

    client = aioredis.from_url(url)
    try:
        print(f"  目标: {url}")
        print(f"  PING: {await client.ping()}")
        info = await client.info("server")
        print(f"  redis_version: {info.get('redis_version')}")
        keys = await client.keys("checkpoint*")
        print(f"  已有 checkpoint* 键: {len(keys)} 个（>0 说明这个库以前用过）")
        return str(info.get("redis_version") or "")
    finally:
        await client.aclose()


async def _step2_capabilities(url: str) -> None:
    """② 能力探测：RediSearch（`FT._LIST`）与 RedisJSON（`JSON.SET`）。"""
    _line("② 能力（RediSearch / RedisJSON）")
    import redis.asyncio as aioredis

    client = aioredis.from_url(url)
    missing: list[str] = []
    try:
        for label, call in (
            ("RediSearch (FT._LIST)", lambda: client.execute_command("FT._LIST")),
            (
                "RedisJSON (JSON.SET)",
                lambda: client.execute_command("JSON.SET", "__probe__", "$", '"1"'),
            ),
        ):
            try:
                await call()
                print(f"  ✅ {label}")
            except Exception as exc:  # noqa: BLE001 - 探测失败即「缺这个能力」
                missing.append(label)
                print(f"  ❌ {label}: {type(exc).__name__}: {str(exc)[:80]}")
        await client.delete("__probe__")
    finally:
        await client.aclose()

    if missing:
        raise CheckpointUnavailable(
            "Redis 缺能力：" + "、".join(missing) + "。\n"
            "  修法（任选其一）：\n"
            "    a) 把 infra/docker-compose.dev.yml 的 redis 镜像换成 redis/redis-stack-server:latest\n"
            "       然后 `docker compose -f infra/docker-compose.dev.yml up -d redis`；\n"
            "    b) 或临时起一个：`docker run -d --name rs -p 6380:6379 redis/redis-stack-server`，\n"
            "       再用 --url redis://<host>:6380/0 跑本脚本。"
        )
    print("  两个模块都在位 —— 可以继续第 ③ 步")


async def _step3_round_trip(backend_url: str) -> None:
    """③ 端到端：真写一个 thread，再**换一个全新 saver** 读回来。"""
    _line("③ 端到端（写 → 换新连接读回）")
    state = customer_state("我的订单到哪了")
    state["messages"] = [
        {"role": "user", "content": "我的订单到哪了"},
        {"role": "assistant", "content": "先前的回答"},
    ]

    async with open_checkpointer("redis", conn_string=backend_url) as saver:
        print(f"  saver = {type(saver).__name__}")
        graph = compile_graph(saver, GraphRuntime(llm=AgentFakeLLM()))
        await graph.aupdate_state(graph_config(PROBE_THREAD), state, as_node=NODE_GUARDRAIL_INPUT)
        snapshot = await graph.aget_state(graph_config(PROBE_THREAD))
        print(f"  写入后 next = {snapshot.next}")

    async with open_checkpointer("redis", conn_string=backend_url) as fresh:
        graph2 = compile_graph(fresh, GraphRuntime(llm=AgentFakeLLM()))
        back = await graph2.aget_state(graph_config(PROBE_THREAD))
        user_id = dict(back.values).get("user_id")
        print(f"  换新连接读回: user_id = {user_id} | next = {back.next}")
        assert user_id == 2, "读不回状态：Redis saver 写进去了但取不出来"
        assert tuple(back.next or ()) == (NODE_ROUTER,), f"断点位置不对：{back.next}"
    print("  ✅ Redis checkpointer 可用（第 8 步的验收项可以勾上）")


async def _amain(url: str | None) -> int:
    ensure_compatible_event_loop()
    from app.config import get_settings

    target = url or get_settings().redis_url
    if not target:
        print("❌ REDIS_URL 为空：请用 --url 指定，或在 .env 里配置 REDIS_URL（见 2.infra-runbook.md）")
        return 1

    try:
        await _step1_connectivity(target)
        await _step2_capabilities(target)
        await _step3_round_trip(target)
    except CheckpointUnavailable as exc:
        print(f"\n❌ 不可用：{exc}")
        print(
            "\n补完之后，仓库里的真库用例是这样跑（它会自动跳过，除非你显式打开开关）：\n"
            "  $env:SHOPSAGE_TEST_REDIS_CHECKPOINTER = \"1\"\n"
            "  uv run pytest -q -p no:cacheprovider -k graph -m db -v"
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - 连不上/协议错都归到这一步的失败
        print(f"\n❌ 失败：{type(exc).__name__}: {exc}")
        print("  若第 ① 步没过，先查地址/端口/防火墙（依赖栈在 Linux VM 上，见 2.infra-runbook.md）")
        return 1

    print("\n完成：Redis 后端可用了。别忘了把镜像改动同步进 infra/docker-compose.dev.yml（如果用了临时容器）。")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="第 8 步待验证项：Redis checkpointer 可用性探针")
    parser.add_argument("--url", default=None, help="覆盖 REDIS_URL（例如临时容器 redis://host:6380/0）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(args.url)))


if __name__ == "__main__":
    main()
