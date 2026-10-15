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

from scripts import demo_accounts

API_BASE = os.environ.get("SHOPSAGE_API_BASE", "http://127.0.0.1:8000")

QUESTIONS = [
    "各个订单状态的订单数量，用柱状图",
    "按订单状态统计订单数量",
    "统计每种订单状态各有多少笔订单，画成柱状图",
    "最近 30 天每天的订单金额趋势，用折线图",
    "销售额最高的 10 个商品，用柱状图",
]


async def main() -> int:
    # 令牌按**真实账号**签发（变更 001 / T14）：真登录上线后鉴权会查 `accounts` 表，
    # 而变更前那种「直接 create_access_token(7, role=...)」签出的是**客户主体**的令牌，
    # 既会被鉴权拒（没有账号行），主体类别也是错的（`c7` 而不是 `e*`）。
    try:
        issued = demo_accounts.issue_for_role("internal_employee")
    except demo_accounts.DemoAccountError as exc:
        print(
            f"[FAIL] 取内部员工令牌失败：{exc}\n"
            "       先灌演示账号：uv run python -m scripts.seed",
            file=sys.stderr,
        )
        return 1
    token = issued.token
    print(f"身份：账号={issued.identifier} 主体={issued.subject} 角色={issued.role} 入口={issued.entry}")
    async with httpx.AsyncClient(base_url=API_BASE, timeout=180) as client:
        # ⚠️ 探活要**自己判**「后端在不在」，不能直接 `health.json()`：
        #    后端没起时（或地址写错、被反代挡掉）拿到的是空响应/HTML 错误页，
        #    `.json()` 会抛 `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` ——
        #    那个报错**完全不指向真正的原因**（实测踩到：以为脚本坏了，其实是没起后端）。
        #    这里把它翻成一句能照做的话，并给一个非 0 退出码，便于脚本化调用。
        try:
            health = await client.get("/health")
            status = health.json().get("status")
        except (httpx.HTTPError, ValueError) as exc:
            print(
                f"❌ 连不上后端 {API_BASE}（{type(exc).__name__}: {exc}）。\n"
                "   本脚本**对着真正在跑的后端**跑（它依赖 lifespan 装配出的图，见文件头说明）。\n"
                "   先起后端，再重跑：\n"
                f"     uv run uvicorn app.main:app --host 127.0.0.1 --port {API_BASE.rsplit(':', 1)[-1]}\n"
                "   地址不对时用环境变量覆盖：$env:SHOPSAGE_API_BASE = 'http://127.0.0.1:8000'",
                file=sys.stderr,
            )
            return 1
        print(f"后端 {API_BASE} → HTTP {health.status_code} status={status}")

        # 逐句的「出不出图」是**模型输出决定的**（analysis 节点要求严格 JSON，偶尔给不出来），
        # 所以同一句话重跑可能不同 —— 结尾汇总出图率，别把单次结果当成回归失败。
        chart_ok = 0
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
                chart_ok += 1
                print(
                    f"  ✅ chart_spec：type={spec['type']} points={spec['meta']['points']} "
                    f"series={[item['name'] for item in spec['series']]} x={spec['x']['field']}"
                )
                print("  data[0]=" + json.dumps(spec["data"][0], ensure_ascii=False))
            else:
                print(f"  ❌ 没有 chart_spec；refusals={body.get('refusals')}")

        print(f"\n=== 出图 {chart_ok}/{len(QUESTIONS)} 句 ===")
        print(
            "⚠️ 这条链路**非确定性**：analysis 节点要求 LLM 严格输出 JSON，"
            "偶尔给不出来就会没图（同一句话重跑结果可能不同）。\n"
            "   判据看的是「失败原因能不能解释」，不是「必须 5/5」。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
