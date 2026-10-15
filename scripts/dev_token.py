"""签发**账号令牌**（第 16 步 Web 端登录用；变更 001 / T14 起**按账号签发**）。

## 它现在是干什么的（与变更 001 之前**不同**）

变更 001 之前，本脚本等价于「一个不需要密码的登录接口」：直接
`create_access_token(user_id, role=...)`，**不查任何账号**。真登录上线后那条路必须收窄，
两个理由（详见 `scripts/demo_accounts` 的模块 docstring）：

1. `create_access_token` 只能签出**客户主体**（`sub="c7"`），于是
   `--role internal_employee` 会签出「员工伪装成客户主体」的令牌 —— 签名验得过，
   但 `thread_id` 按客户派生，与真实员工账号不是同一个会话桶；
2. 鉴权依赖会查 `accounts` 表比对 `ver`/`status`，**没有账号行的令牌一定被拒**。

所以本脚本改成：**先按标识（或角色）查到一个真实账号，再按那个账号签发**。
它因此需要数据库可达 + 演示账号已灌（`uv run python -m scripts.seed`）。

## 用法

```bash
uv run python -m scripts.dev_token                    # 默认：customer 角色的第一个启用账号
uv run python -m scripts.dev_token --account admin     # 指定账号（用户名/手机号）
uv run python -m scripts.dev_token --role admin        # 指定角色的第一个账号
uv run python -m scripts.dev_token --json              # 机器可读（web/tests/run-live.mjs 用）
uv run python -m scripts.dev_token --kind refresh      # 故意签刷新令牌：试 401 的分支
uv run python -m scripts.dev_token --ttl -5            # 故意签过期令牌
```

## ⚠️ 三条纪律（与变更前一致）

1. **只在 `ENV=dev` 下可用**（默认就是 dev）。生产环境拿不到令牌是对的 ——
   本脚本仍然等价于「一个不需要密码的登录接口」（它绕过了口令校验）。
2. **密钥来自 `JWT_SECRET`**（`.env` 注入）。后端进程与本脚本必须在同一份配置下运行，
   否则签名验不过（症状：粘进浏览器后一堆 401）。
3. **令牌里不带权限声明**：权限一律由后端按 `role` 查表（`app/security/rbac.py`），
   令牌只回答「你是谁」。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from app.security.auth import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    AuthError,
    parse_token,
)
from app.security.rbac import ROLE_PERMISSIONS
from scripts.demo_accounts import DemoAccountError, issue_for_identifier, issue_for_role


def build_token(*, role: str, user_id: int, kind: str, ttl_seconds: int | None) -> str:
    """**兼容入口**：按「角色 + user_id」找演示账号再签发（老调用方用）。

    ⚠️ 签名保留是为了不打断既有调用方（`web/tests/run-live.mjs`、runbook 里的命令），
    但它现在**必须先有一个对应账号**。找不到就抛 `DemoAccountError` 并说明怎么建 ——
    旧版「不查账号、直接签」的行为**已被移除**，因为那正是让脚本与真登录脱节的原因。
    """
    del user_id  # 保留形参只为兼容调用方签名；主体由账号行决定（见模块 docstring）
    if role not in ROLE_PERMISSIONS:
        raise DemoAccountError(f"未登记的角色 {role!r}：只接受 {sorted(ROLE_PERMISSIONS)}")
    return issue_for_role(role, kind=kind).token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="签发演示用账号令牌（第 16 步 Web 登录用）")
    parser.add_argument("--account", default=None, help="登录标识（用户名或手机号）；与 --role 二选一")
    parser.add_argument(
        "--role",
        default=None,
        choices=sorted(ROLE_PERMISSIONS),
        help="按角色取第一个启用账号（默认 customer）",
    )
    # 保留旧参数：老命令 `--role x --user-id 7` 仍能跑（user_id 被忽略，主体由账号决定）
    parser.add_argument(
        "--user-id", type=int, default=None, help="[已弃用] 主体由账号行决定，此参数被忽略"
    )
    parser.add_argument(
        "--kind", default=TOKEN_TYPE_ACCESS, choices=[TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH]
    )
    parser.add_argument("--ttl", type=int, default=None, help="有效期（秒）；缺省用 auth 模块的默认值")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = parser.parse_args(argv)

    if os.getenv("ENV", "dev") == "prod":
        print(
            "[FAIL] ENV=prod：本脚本等价于「不需要密码的登录接口」，生产环境禁用。\n"
            "       生产请走 POST /api/auth/login（变更 001 的登录接口）。",
            file=sys.stderr,
        )
        return 1

    try:
        issued = (
            issue_for_identifier(args.account, kind=args.kind, ttl_seconds=args.ttl)
            if args.account
            else issue_for_role(args.role or "customer", kind=args.kind)
        )
    except DemoAccountError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    # ⚠️ 自检对「已过期」的令牌**必须容错**：`parse_token` 在过期时会抛 `AuthError`，
    # 而那正是 `--ttl -5` 想要的结果（用它来演示 401 的分支）。
    try:
        principal = parse_token(issued.token, expected_type=args.kind)
        parsed: dict[str, Any] = {
            "user_id": principal.user_id,
            "subject_id": principal.subject_id,
            "subject_type": principal.subject_type,
            "role": principal.role,
            "token_type": principal.token_type,
            "account_id": principal.account_id,
            "token_version": principal.token_version,
        }
        verify = (
            f"subject={principal.subject} role={principal.role} "
            f"type={principal.token_type} ver={principal.token_version}"
        )
    except AuthError as exc:
        parsed = {"error": str(exc)}
        verify = f"（故意不可用）{exc}"

    payload: dict[str, Any] = {
        "token": issued.token,
        "account": issued.identifier,
        "role": issued.role,
        "subject": issued.subject,
        "account_id": issued.account_id,
        "token_version": issued.token_version,
        "user_id": parsed.get("user_id"),
        "entry": issued.entry,
        "kind": issued.kind,
        "ttl_seconds": issued.ttl_seconds,
        "permissions": issued.permissions,
        "parsed": parsed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(f"账号      = {issued.identifier}（account_id={issued.account_id}）")
    print(f"角色      = {issued.role}（权限点：{', '.join(issued.permissions) or '（无）'}）")
    print(f"主体      = {issued.subject}（thread_id 前缀按它派生：c=客户 / e=员工）")
    print(f"入口      = {issued.entry}")
    print(f"类型/有效期 = {issued.kind} / {issued.ttl_seconds}s（世代号 ver={issued.token_version}）")
    print(f"自检      = parse_token → {verify}")
    print()
    print("令牌（粘到 http://localhost:3000/login；或改用账号+口令登录）：")
    print(issued.token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
