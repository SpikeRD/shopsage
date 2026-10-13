"""人工探针：用内部角色问几个「对内报表」句式，看哪一个能产出 chart_spec。

为什么值得单独跑一次：第 16 步的内对页要展示图表，而**图表是后端 `analysis`
节点在「SQL 真的查回了行」时才生成的**。有些句式会让 LLM 写出带占位符参数
（如 `WHERE created_at >= :since`，而 `since` 的值是
`"<统计起始时间，如 2024-01-01>"`）的 SQL，那条查询会被 Postgres 以
`InvalidDatetimeFormat` 拒掉 —— 于是图表出不来，而界面上看不出是「问法的锅」。
所以先在这里把能用的句式找出来（结论回填 runbook §6.4）。

用法（**对着真正在跑的后端**，因为它依赖 lifespan 装配出的图）：

```bash
uv run python -m scripts.dev_token --json | ...   # 或直接让本脚本自己签
uv run python -m scripts.probe_internal_query
```

⚠️ 不要用 `create_app()` + `httpx.ASGITransport` 直接调：`create_app` 的外部依赖
（图/checkpointer/限流）是在 **lifespan 启动时**挂到 `app.state` 上的，
裸 ASGITransport 不走 lifespan，会得到
`AttributeError: 'State' object has no attribute 'graph_factory'`（实测踩到）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

from app.security.auth import create_access_token

API_BASE = os.environ.get("SHOPSAGE_API_BASE", "http://127.0.0.1:8000")

QUESTIONS = [
    "各个订单状态的订单数量，用柱状图",
    "按订单状态统计订单数量",
    "统计每种订单状态各有多少笔订单，画成柱状图",
    "最近 30 天每天的订单金额趋势，用折线图",
    "销售额最高的 10 个商品，用柱状图",
]


async def main() -> int:
    token = create_access_token(7, role="internal_employee")
    async with httpx.AsyncClient(base_url=API_BASE, timeout=180) as client:
        health = await client.get("/health")
        print(f"后端 {API_BASE} → HTTP {health.status_code} status={health.json().get('status')}")
        for question in QUESTIONS:
            response = await client.post(
                "/api/chat",
                json={"message": question},
                headers={"Authorization": f"Bearer {token}"},
            )
            body = response.json()
            spec = body.get("chart_spec")
            print(f"\n问：{question}")
            print(f"  HTTP={response.status_code} flow_state={body.get('flow_state')} route={body.get('route')}")
            print(f"  回答：{(body.get('answer') or '')[:140]}")
            if spec:
                print(
                    f"  ✅ chart_spec：type={spec['type']} points={spec['meta']['points']} "
                    f"series={[item['name'] for item in spec['series']]} x={spec['x']['field']}"
                )
                print("  data[0]=" + json.dumps(spec["data"][0], ensure_ascii=False))
            else:
                print(f"  ❌ 没有 chart_spec；refusals={body.get('refusals')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
