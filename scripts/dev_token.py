"""签发**演示用**访问令牌（第 16 步 Web 端登录用）。

为什么需要它：本仓没有登录接口 —— 第 9 步只交付了「令牌怎么被验」
（`app/security/auth.py::parse_token`），签发工具 `create_access_token` 一直是
测试与演示脚本在使用（见 `scripts/api_demo.py::_tokens`）。
Web 端要真的连上后端，就需要一个**能粘进浏览器**的令牌，于是有了这条命令。

用法：

```bash
uv run python -m scripts.dev_token                          # 默认：客户, user_id=2, 30 分钟
uv run python -m scripts.dev_token --role internal_employee --user-id 7
uv run python -m scripts.dev_token --json                   # 机器可读（给 web/tests/run-live.mjs 用）
uv run python -m scripts.dev_token --kind refresh           # 故意签刷新令牌：用来试 401 的分支
```

⚠️ 三条纪律：

1. **只在 `ENV=dev` 下可用**（默认就是 dev）。生产环境拿不到令牌是对的 ——
   这个脚本等价于「一个不需要密码的登录接口」；
2. **密钥来自 `JWT_SECRET`**（`.env` 注入）。没配时用开发默认密钥
   （与 `app/security/auth.py::_DEV_SECRET` 同一个值），所以**后端进程与这个脚本
   必须在同一份配置下运行**，否则签名验不过（症状：粘进浏览器后一堆 401）；
3. **令牌里不带任何额外声明**（没有 `permissions`）—— 权限一律由后端按 `role` 查表
   （`app/security/rbac.py`），令牌只回答「你是谁」。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.security.auth import (
    AuthError,
    DEFAULT_REFRESH_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    parse_token,
)
from app.security.rbac import ROLE_PERMISSIONS


def build_token(*, role: str, user_id: int, kind: str, ttl_seconds: int | None) -> str:
    """按类型签发令牌（`access` 走 `create_access_token`，`refresh` 走另一个函数）。

    ⚠️ **`ttl_seconds=None` 时必须把参数省掉**，不能显式传 `None`：
    `app/security/auth.py::_create_token` 里是
    `timedelta(seconds=ttl_seconds)` —— 传 `None` 会直接 `TypeError`
    （实测踩到，见 runbook §10.1 坑 2）。这个坑很隐蔽：函数签名写的是
    `ttl_seconds: int = DEFAULT_TTL_SECONDS`，看起来「传 None 就等于用默认值」，
    但默认值只在**不传**时生效。
    """
    kwargs = {} if ttl_seconds is None else {"ttl_seconds": ttl_seconds}
    if kind == TOKEN_TYPE_REFRESH:
        return create_refresh_token(user_id, **kwargs)
    return create_access_token(user_id, role=role, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="签发演示用 JWT（第 16 步 Web 登录用）")
    parser.add_argument("--role", default="customer", choices=sorted(ROLE_PERMISSIONS), help="角色（决定权限点）")
    parser.add_argument("--user-id", type=int, default=2, help="用户 id（行级过滤的依据，种子数据里 1..8）")
    parser.add_argument("--kind", default=TOKEN_TYPE_ACCESS, choices=[TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH])
    parser.add_argument("--ttl", type=int, default=None, help="有效期（秒）；缺省用 auth 模块的默认值")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = parser.parse_args(argv)

    token = build_token(role=args.role, user_id=args.user_id, kind=args.kind, ttl_seconds=args.ttl)
    # ⚠️ 自检对「已过期」的令牌**必须容错**：`parse_token` 在过期时会抛 `AuthError`，
    # 而那正是 `--ttl -5` 想要的结果（用它来演示 401/4401 的分支）。
    # 第一版没兜这条，于是「故意签一个过期令牌」变成了「脚本崩掉」——见 runbook §10.1 坑 3。
    try:
        principal = parse_token(token)
        parsed = {
            "user_id": principal.user_id,
            "role": principal.role,
            "token_type": principal.token_type,
        }
        verify = f"user_id={principal.user_id} role={principal.role} type={principal.token_type}"
    except AuthError as exc:
        parsed = {"error": str(exc)}
        verify = f"（故意不可用）{exc}"
    permissions = sorted(ROLE_PERMISSIONS.get(args.role, frozenset()))
    ttl = args.ttl or (DEFAULT_REFRESH_TTL_SECONDS if args.kind == TOKEN_TYPE_REFRESH else DEFAULT_TTL_SECONDS)

    payload: dict[str, Any] = {
        "token": token,
        "role": args.role,
        "user_id": args.user_id,
        "kind": args.kind,
        "ttl_seconds": ttl,
        "permissions": permissions,
        "parsed": parsed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(f"角色      = {args.role}（权限点：{', '.join(permissions) or '（无）'}）")
    print(f"user_id   = {args.user_id}（行级过滤的依据：只能查自己的数据）")
    print(f"类型/有效期 = {args.kind} / {ttl}s")
    print(f"自检      = parse_token → {verify}")
    print()
    print("令牌（粘到 http://localhost:3000/login）：")
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
