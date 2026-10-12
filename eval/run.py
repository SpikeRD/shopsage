"""`python -m eval.run` 的入口 —— **文档 §2 的那条命令**。

实现全部在 `app/eval/run.py`；本文件只做转发（理由见 `eval/__init__.py`）。
"""

from __future__ import annotations

import sys

from app.eval.run import main

if __name__ == "__main__":
    sys.exit(main())
