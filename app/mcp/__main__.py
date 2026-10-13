"""`python -m app.mcp` 的入口（与 `python -m app.mcp.server` 等价）。

⚠️ 这里**只有 3 行**：真正的逻辑在 `server.main()`。
薄壳里一旦出现「顺手处理一下」，两个入口就会出现两种行为 ——
而它们是同一件事，必须完全一致（与第 14 步 `eval/run.py` 的纪律相同）。
"""

import sys

from app.mcp.server import main

sys.exit(main())
