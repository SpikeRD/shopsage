"""三条主路径的端到端回归（变更 002 / T18）—— 一次性取证脚本，不是测试套件。

为什么要写成文件而不是 PowerShell 一行命令：**中文请求体经 PowerShell 传递会变乱码**
（实测 `我的订单到哪了？` 到后端变成了 `???????`，后端如实回答「问题内容为乱码」）。
这不是后端问题，是 shell 编码问题 —— 所以取证脚本必须自己控制 UTF-8 编码。

用法：

    uv run python -m scripts.evidence_paths
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000"


def login(identifier: str, password: str) -> str:
    payload = json.dumps({"identifier": identifier, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        f"{API}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))["access_token"]


def call(path: str, token: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def main() -> int:
    # 口令取自 scripts/seed.py 的 DEMO_ACCOUNTS（演示口令，刻意不打印到终端）
    customer = login("13800000002", "customer-demo-pass")
    employee = login("employee", "employee-demo-pass")
    admin = login("admin", "admin-demo-pass")

    results: list[tuple[str, bool, str]] = []

    status, payload = call("/api/chat", customer, {"message": "我的订单到哪了？", "session_id": "evcustomer"})
    answer = str(payload.get("answer", ""))
    print("=== 路径 1：对客问答（customer）===")
    print(f"HTTP {status} flow_state={payload.get('flow_state')} route={payload.get('route')}")
    print(f"answer: {answer[:200]}")
    results.append(("对客问答", status == 200 and bool(answer), f"HTTP {status}"))
    print()

    status, payload = call(
        "/api/chat", employee, {"message": "各个订单状态的订单数量，用柱状图", "session_id": "evinternal"}
    )
    answer = str(payload.get("answer", ""))
    chart = payload.get("chart_spec")
    print("=== 路径 2：对内查询（employee）===")
    print(f"HTTP {status} flow_state={payload.get('flow_state')} route={payload.get('route')}")
    print(f"answer: {answer[:200]}")
    print(f"chart_spec: {'有（' + str(chart.get('type')) + '）' if isinstance(chart, dict) else '无'}")
    results.append(("对内查询", status == 200 and bool(answer), f"HTTP {status}"))
    print()

    status, payload = call("/api/admin/accounts", admin)
    accounts = payload.get("accounts", [])
    print("=== 路径 3：管理台（admin 列账号）===")
    print(f"HTTP {status} 账号数={len(accounts) if isinstance(accounts, list) else '?'}")
    results.append(("管理台列账号", status == 200, f"HTTP {status}"))
    print()

    status, _ = call("/api/admin/accounts", customer)
    print("=== 越权对照：customer 调管理台（预期 404）===")
    print(f"HTTP {status}")
    results.append(("越权被拒（404）", status == 404, f"HTTP {status}"))
    print()

    print("=== 汇总 ===")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<20} {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
