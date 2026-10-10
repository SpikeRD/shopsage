"""`tests/graph/` 的局部装置：**在任何事件循环被创建之前**把 Windows 的循环策略摆正。

为什么需要它（实测，见 `docs/runbook/8.graph-runbook.md` §10.1 坑 1）：

* psycopg 的异步模式**不能在 `ProactorEventLoop` 上跑**；
* Windows 上 `asyncio.run()` 与 pytest-asyncio **默认就是 Proactor**；
* 事件循环策略是「新建循环时读一次」的全局量 —— 循环已经建好再改，对已有循环无效。

所以它必须发生在 **conftest 的 import 期**（pytest 收集到这个目录时），而不是 fixture 里。
放在 `tests/graph/` 而不是顶层 `tests/conftest.py`：这是**图这一层**的运行环境要求，
不该改变其余 600+ 条既有用例的事件循环（它们现在都在 Proactor 上跑得好好的）。

生产（Linux）上 `ensure_compatible_event_loop()` 是空操作，所以这里不是「为测试而特判」，
而是把生产入口该做的那一步提前做掉。
"""

from __future__ import annotations

from app.graph.checkpointer import ensure_compatible_event_loop

ensure_compatible_event_loop()
