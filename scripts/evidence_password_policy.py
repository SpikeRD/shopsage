"""需求 3 取证脚本（issue 004）：管理接口对**口令强度**的判定确实生效。

一次性取证脚本，不是测试套件。跑法（需要后端在跑）：

    uv run uvicorn app.main:app --port 8000     # 另开一个终端
    uv run python -m scripts.evidence_password_policy

## 它回答的问题

`issues/003` 的 P1-3 给口令加了强度校验（≥ `accounts.MIN_PASSWORD_LENGTH` 位）。
「策略写在那里」不等于「它真的在生效」—— 本脚本**走真 HTTP** 把边界走一遍：

```text
口令 11 位 -> 必须 422（差一位也不行）
口令 12 位 -> 必须 201
```

⚠️ 本脚本会在**成功**那条路径上真的建一个账号，跑完自己删掉。
它只该对着**开发/演示库**跑（与 `scripts/seed.py` 同一条纪律）。

⚠️ 它**不打印任何口令明文**（只打印长度与判定结果）——
理由与 `accounts.reset_password` 的 docstring 同源：凭据一旦进终端输出就等于泄露。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.security.accounts import MIN_PASSWORD_LENGTH

DEFAULT_BASE = "http://127.0.0.1:8000"
DEMO_ADMIN = "admin"
#: 演示管理员口令（与 `scripts/seed.py::DEMO_ACCOUNTS` 同源）。
DEMO_ADMIN_PASSWORD = "admin-demo-pass"

#: 建号时借用的主体 —— 客户 8 是种子里最后一个，通常没有被账号占用。
PROBE_SUBJECT_ID = 8


def _call(
    method: str, path: str, *, base: str, body: dict | None = None, token: str = ""
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{base}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw}


def main() -> int:
    base = DEFAULT_BASE

    status, login = _call(
        "POST", "/api/auth/login", base=base,
        body={"identifier": DEMO_ADMIN, "password": DEMO_ADMIN_PASSWORD},
    )
    if status != 200:
        print(f"[FAIL] 管理员登录失败（HTTP {status}）：后端在跑吗？演示账号灌了吗？")
        print("       起后端：uv run uvicorn app.main:app --host 0.0.0.0 --port 8000")
        print("       灌账号：uv run python -m scripts.seed")
        return 1
    token = login["access_token"]

    print(f"口令长度下限（accounts.MIN_PASSWORD_LENGTH） = {MIN_PASSWORD_LENGTH}")
    print()
    print(f"{'用例':<22}{'口令长度':<10}{'期望':<8}{'实测':<8}判定")
    print("-" * 60)

    results: list[tuple[str, bool]] = []
    created_id: int | None = None

    # 边界向量：差一位必须被拒，正好够长必须放行
    cases: list[tuple[str, str, int]] = [
        ("差一位", "x" * (MIN_PASSWORD_LENGTH - 1), 422),
        ("正好够长", "x" * MIN_PASSWORD_LENGTH, 201),
    ]

    for index, (label, password, expected) in enumerate(cases):
        status, payload = _call(
            "POST", "/api/admin/accounts", base=base, token=token,
            body={
                "identifier": f"zz-evidence-pw-{index}",
                "password": password,
                "role": "customer",
                "subject_type": "customer",
                "subject_id": PROBE_SUBJECT_ID,
                "status": "active",
            },
        )
        ok = status == expected
        results.append((label, ok))
        print(f"{label:<22}{len(password):<10}{expected:<8}{status:<8}{'PASS' if ok else 'FAIL'}")
        if status == 201:
            created_id = int(payload.get("account_id", 0)) or None

        # 顺带把「字段级原因」也打出来 —— 这正是前端 issue 004 要渲染的那份信息
        if status == 422:
            fields = payload.get("error", {}).get("detail", {}).get("fields", [])
            for item in fields:
                loc = ".".join(str(x) for x in item.get("loc", []))
                print(f"      └ 422 字段级原因：{loc} → {item.get('msg')}")

    # 清理：把成功那条路径建的账号停用（管理接口没有删除，停用即退出可用态）
    if created_id:
        status, _ = _call(
            "PATCH", f"/api/admin/accounts/{created_id}", base=base, token=token,
            body={"status": "disabled"},
        )
        print(f"\n清理：停用探针账号 #{created_id}（HTTP {status}）")

    ok = all(item[1] for item in results)
    print()
    print("VERDICT =", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
