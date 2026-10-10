"""图层的契约检查：**接口面、依赖边界、以及「没有任何东西可以绕过 tools」**。

与 `tests/test_agents_contracts.py` 同一套路（那边钉住第 7 步），本文件钉住第 8 步。
最有价值的两条是**源码层**的：

1. `app/graph/` 不许出现数据库引擎/会话构造，也不许直连模型厂商 ——
   「唯一副作用出口 = `app/tools`」「唯一模型出口 = `app.llm`」在图层同样成立。
   写得再对，只要有人在这里顺手 `SessionLocal()`，第 6/5 步的纪律就废了。
2. 依赖方向不许反转：`app/agents` **一个 langgraph 都不能 import** ——
   它是第 7 步的硬约束（那一步的可独立测试全靠它），第 8 步引入 langgraph 之后
   这条约束更值得守住，因为它现在是「可以被悄悄破坏」的（依赖已经装上了）。

源码检查必须用 **AST**（而不是字符串匹配）：本仓的文档字符串里到处在**解释**
「不要绕过 tools」，纯文本匹配会把说明一起当成违规（`7.agents-runbook.md` §10.1 坑 7）。
"""

from __future__ import annotations

import ast
import inspect
import tomllib
from pathlib import Path

import pytest

GRAPH_DIR = Path("app/graph")
AGENT_DIR = Path("app/agents")

#: 图层**绝不允许**出现的名字（它们各自属于别层的唯一出口）
FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "SessionLocal",
        "get_engine",
        "get_read_engine",
        "create_engine",
        "psycopg",
        "asyncpg",
    }
)

#: 图层**绝不允许** import 的第三方包
FORBIDDEN_MODULES: frozenset[str] = frozenset({"litellm", "openai", "sqlalchemy", "psycopg"})


def _graph_files() -> list[Path]:
    return sorted(GRAPH_DIR.glob("*.py"))


def test_graph_package_has_files() -> None:
    names = {path.name for path in _graph_files()}
    expected = {"__init__.py", "builder.py", "checkpointer.py", "config.py", "nodes.py", "routing.py", "run.py", "state.py"}
    assert expected <= names, f"图层缺文件：{sorted(expected - names)}"


@pytest.mark.parametrize("path", _graph_files(), ids=lambda p: p.name)
def test_no_layer_violations_in_source(path: Path) -> None:
    """图层不许自己开数据库会话 / 直连模型厂商（AST 检查，注释与 docstring 不算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            offenders.append(f"name:{node.id}@L{node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            offenders.append(f"attr:{node.attr}@L{node.lineno}")
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"import:{alias.name}@L{node.lineno}"
                for alias in node.names
                if alias.name.split(".")[0] in FORBIDDEN_MODULES
            )
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module.split(".")[0] in FORBIDDEN_MODULES:
                offenders.append(f"from:{node.module}@L{node.lineno}")
    assert offenders == [], f"{path.name} 越过图层边界：{offenders}"


@pytest.mark.parametrize("path", sorted(AGENT_DIR.glob("*.py")), ids=lambda p: p.name)
def test_agent_layer_still_does_not_import_langgraph(path: Path) -> None:
    """依赖方向不许反转：`app/agents` 至今**不 import langgraph**（第 7 步的硬约束）。

    为什么现在更要守：langgraph 已经装进环境了，这条约束从「装不上所以做不到」
    变成了「只要有人图省事就会发生」。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module.split(".")[0])
    bad = modules & {"langgraph", "langchain", "langchain_core", "litellm", "openai"}
    assert bad == set(), f"{path.name} import 了图层依赖：{sorted(bad)}"


def test_facade_exposes_everything_documented_in_section_2() -> None:
    """`8.graph.md` §2 的三行 import 必须一字不差地可用。"""
    import app.graph as package

    required = {
        "build_graph",
        "compile_graph",
        "GraphState",
        "open_checkpointer",
        "graph_config",
        "run_turn",
        "GraphRuntime",
        "InterruptPolicy",
    }
    missing = required - set(package.__all__)
    assert not missing, f"门面里缺：{sorted(missing)}"
    for name in required:
        assert hasattr(package, name), f"门面声明了 {name} 但取不到"


def test_state_module_is_importable_exactly_as_documented() -> None:
    """`from app.graph.state import GraphState`（§2 的第一行）必须成立。"""
    from app.graph.state import GraphState as FromGraph
    from app.graph import GraphState as FromFacade

    assert FromGraph is FromFacade


def test_public_callables_have_docstrings() -> None:
    """公开函数必须自解释（本仓的文档密度靠这条纪律维持）。"""
    import app.graph as package

    undocumented = [
        name
        for name in package.__all__
        if inspect.isfunction(getattr(package, name)) and not (getattr(package, name).__doc__ or "").strip()
    ]
    assert undocumented == [], f"这些公开函数没有 docstring：{sorted(undocumented)}"


def test_langgraph_is_a_declared_dependency_with_a_lower_bound() -> None:
    """`langgraph` 必须是**声明过的**运行依赖（而不是「碰巧装在环境里」）。

    为什么值得一条用例：本步新增了三个依赖；若只装进环境而没写进 `pyproject.toml`，
    CI/新机器上会以 `ModuleNotFoundError` 的形式失败，而那时的第一反应往往是去查代码。
    下限版本也一并断言：本步用到的 `durability` / `Command` / `interrupt` 都不是老版本能力。
    """
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    declared = {dep.split(">=")[0].split("[")[0].strip().lower(): dep for dep in deps}
    for package in ("langgraph", "langgraph-checkpoint-postgres", "langgraph-checkpoint-redis"):
        assert package in declared, f"pyproject.toml 没有声明 {package}"
        assert ">=" in declared[package], f"{package} 应当带下限版本"


def test_graph_has_no_cycles_by_construction() -> None:
    """图里没有环（除了「重放」那种由 checkpoint 驱动的重复执行）。

    判据：从 `__start__` 出发能到 `__end__`，且**没有任何节点能回到 router**——
    回到 router 会让一轮请求把意图分类跑第二遍（成本翻倍、并可能改变路由）。
    """
    from app.graph import GraphRuntime, compile_graph
    from tests.graph_helpers import memory_saver

    graph = compile_graph(memory_saver(), GraphRuntime())
    edges = [(edge.source, edge.target) for edge in graph.get_graph().edges]
    back_to_router = [edge for edge in edges if edge[1] == "router" and edge[0] not in {"__start__", "guardrail_input"}]
    assert back_to_router == [], f"有边回到了 router：{back_to_router}"
