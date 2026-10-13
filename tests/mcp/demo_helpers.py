"""`tests/mcp/` 的公共帮助：**起子进程时显式构造环境**。

为什么单独一个文件（本步实测踩到的坑，runbook §10.1 坑 12）：

第一版子进程用例直接 `env={**os.environ, ...}` —— 于是子进程继承了**我 shell 里的**
`PYTHONUTF8=1`，用例全绿；而用户在自己的 cp936 shell 里跑同一条命令时，
演示脚本因为 `print("✅ …")` 编不出来而崩在 `UnicodeEncodeError` 上。

一句话：**继承父进程环境 = 用例在测「我的机器」，不是在测「用户的机器」**。
所以这里只做一件事：把子进程环境显式构造出来，并且把「输出编码」当成一个**自变量**
（`utf-8` 与 `gbk` 两档），让两条路径都被测到。

⚠️ 读回子进程输出时也要按**同一个编码**解码：一个 cp936 的字节流被当成 UTF-8 解码会乱码，
那种失败的原因在父进程的 `encoding=` 上，与子进程无关（会让排查方向整个跑偏）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: 仓库根：`python -m <module>` 必须在根目录跑（`app` 包从根解析，`uv run` 不改工作目录）
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: 两种「标准流编码」环境。`gbk` 不是随便挑的：它是 Windows 中文机的默认控制台代码页
#: （cp936），也就是用户第一次跑演示脚本时会遇到的那个环境。
IO_ENCODINGS: tuple[str, ...] = ("utf-8", "gbk")


def child_env(io_encoding: str = "utf-8", **extra: str) -> dict[str, str]:
    """显式构造子进程环境：**不继承** `PYTHONUTF8` / `PYTHONIOENCODING`。

    参数：
        io_encoding: 子进程标准流的编码（用 `PYTHONIOENCODING` 指定），
            取值见 `IO_ENCODINGS`。这个参数就是「用户 shell 长什么样」的自变量。
        extra: 额外要设置的环境变量（例如 `MCP_ACTOR_USER_ID`）。
    """
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONIOENCODING"] = io_encoding
    env.update(extra)
    return env


def run_module(module: str, *args: str, io_encoding: str = "utf-8", **env: str):
    """在子进程里跑一个 `python -m <module>`，用与子进程一致的编码读回输出。

    返回 `subprocess.CompletedProcess[str]`（`stdout` / `stderr` 是 `str`）。
    `errors="replace"` 是兜底：编码问题本身由断言负责暴露，不该让读取阶段先炸。
    """
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=child_env(io_encoding, **env),
        encoding=io_encoding,
        errors="replace",
        timeout=180,
    )


__all__ = ["IO_ENCODINGS", "REPO_ROOT", "child_env", "run_module"]
