"""`python -m app.eval` 的入口（等价于 `python -m app.eval.run`）。

存在的理由只有一条：让**两种写法都能跑**。
`14.eval.md` §2 写的是 `python -m eval.run`（仓库根的薄壳包），
而本仓的实现放在 `app/eval/` —— 少了这个文件，习惯敲 `python -m app.eval` 的人
会看到一句 `No module named app.eval.__main__`，然后去翻文档找那层薄壳在哪。
"""

from __future__ import annotations

import sys

from app.eval.run import main

if __name__ == "__main__":
    sys.exit(main())
