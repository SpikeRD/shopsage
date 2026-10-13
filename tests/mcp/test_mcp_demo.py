"""演示脚本与 CLI 的回归用例：**在「难看」的 shell 里也必须退出码 0**。

为什么要给一个「演示脚本」写用例，而且要刻意模拟坏环境（runbook §10.1 坑 11 / 坑 12）——

1. `scripts/mcp_demo.py` 的 `guard` 场景第一版把「缺行级过滤 / 字面量过滤」当成两种策略下
   都必须被拦的硬断言，于是 `--policy internal` 必然报 ❌。
   **演示脚本的预期也是一份断言**，它接受配置参数却只按一种配置写过预期。
2. 更隐蔽的一个：脚本输出里有 ✅ / ❌ / ➖，而 Windows 上 Python 默认按**控制台代码页**
   （中文机器 = cp936）编码标准输出 —— `print` 直接在 cp936 shell 里抛
   `UnicodeEncodeError`，表现为「所有场景都报 ❌ 且输出乱码」。
   第一版用例之所以全绿，是因为**我自己的 shell 里设了 `PYTHONUTF8=1`**，
   子进程继承了这个变量 —— 用例测的是「我的环境」，不是「用户的环境」。

所以这里的装置有一条硬纪律：**子进程环境是显式构造的，不读外层的 `PYTHONUTF8`**。
两条用例分别模拟：
* `utf-8`（CI / 已经把环境配好的机器）；
* `gbk`（Windows 默认的 cp936 shell，用户第一次跑就会遇到的那个）。
两者都必须：退出码 0、输出里能读到 ✅/❌ 这些字符、stderr 里没有 `UnicodeEncodeError`。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.mcp.demo_helpers import IO_ENCODINGS, run_module

#: 零依赖的三种场景（`stdio` 会起子进程，但不需要库）
CHEAP_SCENARIOS = ("discover", "guard", "stdio")


def _run_demo(*args: str, io_encoding: str = "utf-8"):
    """跑一次演示脚本（子进程环境由 `demo_helpers.child_env` 显式构造）。"""
    return run_module("scripts.mcp_demo", *args, io_encoding=io_encoding)


@pytest.mark.parametrize("policy", ["customer", "internal"])
@pytest.mark.parametrize("io_encoding", IO_ENCODINGS)
def test_cheap_scenarios_pass_in_any_shell(policy, io_encoding):
    """★两种策略 × 两种输出编码：零依赖场景都必须退出码 0、零个 ❌。

    两个自变量各有来历（都实测踩过）：
    * `policy` —— 预期必须随策略切换（坑 11）；
    * `io_encoding` —— 输出编码不许让脚本崩（坑 12）。
    """
    for scenario in CHEAP_SCENARIOS:
        done = _run_demo(
            "--policy", policy, "--scenario", scenario, io_encoding=io_encoding
        )
        assert done.returncode == 0, (
            f"策略 {policy} / 编码 {io_encoding} 下场景 {scenario} 退出码 {done.returncode}\n"
            f"stdout:\n{done.stdout}\nstderr:\n{done.stderr[-2000:]}"
        )
        assert "❌" not in done.stdout, f"策略 {policy} / 编码 {io_encoding}：\n{done.stdout}"
        assert "UnicodeEncodeError" not in done.stderr, (
            f"编码 {io_encoding} 下脚本崩在 print 上（stdout 编码问题）：\n{done.stderr[-1500:]}"
        )


def test_customer_policy_blocks_the_row_filter_cases():
    """对客策略：行级过滤那两条必须是 `BLOCK[...]`（本步新增的那一层在做它的工作）。"""
    done = _run_demo("--policy", "customer", "--scenario", "guard")
    assert done.returncode == 0
    assert "BLOCK[mcp_missing_row_filter]" in done.stdout
    assert "BLOCK[mcp_row_filter_needs_binding]" in done.stdout
    assert "➖" not in done.stdout, "对客策略下不该有「按设计放行」的行"


def test_internal_policy_reports_the_row_filter_cases_as_by_design():
    """内部策略：那两条要**明确说成「按设计放行」**，而不是 ❌ 或悄悄消失。

    这一条测的是**可读性**：放宽是"设计如此"，运维必须能从输出里看出这件事，
    而不是自己推断（runbook §4.3 的风险清单就是要求把这条写进部署文档）。
    """
    done = _run_demo("--policy", "internal", "--scenario", "guard")
    assert done.returncode == 0
    assert done.stdout.count("➖") == 2, f"两条放宽项都要显式标出：\n{done.stdout}"
    assert "按设计放行" in done.stdout
    assert "mcp_missing_row_filter" not in done.stdout, "内部策略下这条规则不该被触发"
    # 但守卫那四条**必须照旧被拦**：内部策略放宽的是"行范围"，不是"安全检查"
    for rule in ("not_read_only", "multiple_statements", "missing_where", "table_not_allowed"):
        assert f"BLOCK[{rule}]" in done.stdout, f"内部策略下 {rule} 仍然必须拦住"


def test_demo_json_summary_is_machine_readable():
    """`--json` 的摘要能被 `json.loads` 直接吃（CI 用它，不能混进人类可读的行）。"""
    done = _run_demo("--json", "--scenario", "guard")
    assert done.returncode == 0
    payload: Any = None
    for line in done.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
    assert payload is not None, f"--json 模式下没有输出 JSON 摘要：\n{done.stdout}"
    assert payload["ok"] is True
    assert set(payload["scenarios"]) == {"guard"}


def test_demo_help_lists_every_scenario():
    """`--help` 里的场景名与实际实现一致（删/改场景名时能立刻发现）。"""
    done = _run_demo("--help")
    assert done.returncode == 0
    for scenario in (*CHEAP_SCENARIOS, "offline", "query", "retrieve", "all"):
        assert scenario in done.stdout, f"--help 里少了 {scenario}"
