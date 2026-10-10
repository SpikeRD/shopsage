"""`uvicorn` 的入口模块（`app.main:app`）——**只做一件事：把 app 装出来**。

为什么入口文件在 `app/` 根、而装配逻辑在 `app/api/main.py`：

* `app.main:app` 是 uvicorn 的**约定路径**（`uvicorn app.main:app`），它必须存在；
* 装配逻辑属于第 9 步的模块（`app/api/`），入口只是它的一个引用 ——
  这样「API 层怎么装」只有一处真相，测试可以直接 `create_app(...)` 装配带假模型的 app，
  不必 import 这个模块（那条路径上会读 `.env`、会尝试连 Redis）。

```powershell
# 开发（单进程，改代码自动重启）
uv run uvicorn app.main:app --reload --port 8000

# 生产口径（多副本；会话状态在 checkpointer 里，所以可以直接加 worker）
uv run uvicorn app.main:app --host 0.0.0.0 --workers 4 --port 8000
```

⚠️ checkpointer 后端由 `CHECKPOINT_BACKEND` 决定（默认 `memory`，见 `app/api/config.py`）。
`memory` 意味着**进程重启即丢会话**，只在开发/演示时用；生产要设 `CHECKPOINT_BACKEND=postgres`
（状态与业务库同处，`8.graph-runbook.md` §4 已实测落库）。
"""

from __future__ import annotations

from app.api import create_app
from app.api.config import checkpoint_backend

app = create_app(backend=checkpoint_backend())

__all__ = ["app"]
