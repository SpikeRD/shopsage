r"""可观测性纪律的源码级自检（第 11 步）：把三条「写错了不报错」的约定变成可执行检查。

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run python -m scripts.check_observability
```

## 为什么需要它（而不是在 runbook 里写一条 grep）

本步的三条纪律**全部属于「违反了也没有任何症状」**：

| 纪律 | 违反后的症状 | 为什么测试抓不到 |
|---|---|---|
| 指标标签里不许出现 `user_id` 等 | 一人一条时间序列（基数爆炸），Prometheus 内存先扛不住 | 功能完全正常，只有在真实流量下才暴露（那时已经晚了） |
| 业务代码不许自己拼 span 名 | 拼错一个字母 → 那条 span 的耗时**静默地不进任何指标** | 系统照跑，只是那个节点的 P99 永远没有数据 |
| span 属性必须过白名单 | prompt / SQL 全文出网（进 trace UI、日志、外部 Langfuse） | 数据照样产生，只是**多了一些不该有的**东西 |

## 本脚本的两条纪律（第一版的两条 grep 都是错的，都换成了 AST）

**第一版的两条 grep 是错的**，写在 runbook 里会误导人 —— 记在这里备查：

1. **正则匹配 `"user_id":`** 会产生 15 处**假阳性**：`app/agents/state.py` 的
   `{"user_id": ...}` 是**图状态的键**，`app/tools/ticket.py` 的是**插入的列**，
   `app/observability/langfuse_cb.py` 的是 **Langfuse 的 metadata**（不是指标标签）——
   它们都不是「指标标签」，全都是合法的。
   真正的判据是「**这个 dict 是不是作为 label 参数传给了指标的写入口**」，
   而那需要**语法结构**（AST），不是正则能表达的。
2. **正则匹配 `trace_span(`** 会对**合法的**直接调用报错（本步第一版就报了
   `app/graph/nodes.py` 与 `app/llm/client.py`）。现在的做法是**在实现上消除**这类调用
   （它们改走 `instrument.node_span` / `embed_span` / `llm_span`），于是这条检查
   变成一条**真正的**纪律（允许列表为空），而不是「有例外所以要靠人记」。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

#: **绝不允许**出现在指标标签里的键。三条理由各不同：
#:   * `user_id` / `session_id` / `thread_id` —— 基数爆炸（每个用户/会话一条序列）；
#:   * `request_id` —— 同上，而且它还是**每次请求都新**的一个值（最坏的一类标签）；
#:   * `sql` / `prompt` / `query` / `message` —— 值可能很长且含 PII（标签值要出网）。
FORBIDDEN_METRIC_LABELS: frozenset[str] = frozenset(
    {
        "user_id",
        "session_id",
        "thread_id",
        "request_id",
        "trace_id",
        "span_id",
        "sql",
        "prompt",
        "query",
        "message",
        "params",
        "answer",
    }
)

#: 指标的**写入口**方法名（标签参数是它们的第 2 个位置参数或 `labels=`）。
METRIC_WRITE_METHODS: frozenset[str] = frozenset({"counter", "observe"})

#: 允许直接调用 `tracing.trace_span` 的**路径**（除了 `app/observability/` 本身）。
#:
#: ⚠️ 刻意为**空**：span 名的构造规约只有 `app/observability/instrument.py` 一处
#: （它是「实现层」，负责把名字拼对；其余模块只调它的具名入口）。
#: 本步第一版这里需要两条例外（`app/graph/nodes.py` 与 `app/llm/client.py` 直接调了
#: `trace_span`）—— 那不是「必须的例外」，而是**接口没做完**：
#: 例外列表每多一条，纪律就多一个需要人记的漏洞，所以正确做法是补上具名入口
#: （`node_span` / `embed_span` / `llm_span`）并把例外清空。
ALLOWED_DIRECT_SPAN_CALLERS: tuple[str, ...] = ()

#: span 名的构造者（**唯一**允许直接调 `tracing.trace_*` 的模块）。
SPAN_NAME_OWNER = "app/observability/instrument.py"

#: 允许出现在 span 属性里的键 —— **必须与 `tracing.ALLOWED_ATTRIBUTES` 一致**。
SPAN_ATTRIBUTE_WHITELIST = "app/observability/tracing.py"


class Problem:
    def __init__(self, kind: str, where: str, detail: str, fix: str) -> None:
        self.kind = kind
        self.where = where
        self.detail = detail
        self.fix = fix

    def __str__(self) -> str:
        return f"[{self.kind}] {self.where}\n    {self.detail}\n    → 修法：{self.fix}"


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _read_source(path: Path) -> tuple[str, str]:
    """读源码，返回 `(文本, 错误说明)`；**读不了就让调用方记一个问题而不是崩**。

    ⚠️ 为什么需要它（本步实测踩到）：`utf-8` **带 BOM** 的文件（Windows 上用
    `Set-Content -Encoding utf8` / 某些编辑器保存出来就是）会让
    `ast.parse` 抛 `SyntaxError: invalid non-printable character U+FEFF`。
    第一版直接让异常冒出去，于是**整条自检脚本崩掉**、一条问题都没报出来 ——
    而「自检脚本自己崩了」比「没检查」更糟（它看起来像配置问题）。
    现在用 `utf-8-sig` 读（顺带吃掉 BOM），真读不了就记成一条问题。
    """
    try:
        return path.read_text(encoding="utf-8-sig"), ""
    except (UnicodeDecodeError, OSError) as exc:
        return "", f"{type(exc).__name__}: {exc}"


def _parse(path: Path, relative: str) -> tuple[ast.Module | None, list["Problem"]]:
    """解析一个文件；解析失败时返回一条问题（而不是抛异常）。"""
    source, error = _read_source(path)
    if error:
        return None, [
            Problem(
                "源码可读性",
                relative,
                f"读不了这个文件：{error}",
                "检查文件编码（自检脚本按 utf-8-sig 读，能吃掉 BOM）与权限。",
            )
        ]
    try:
        return ast.parse(source), []
    except SyntaxError as exc:
        return None, [
            Problem(
                "源码可解析性",
                f"{relative}:{exc.lineno}",
                f"ast.parse 失败：{exc.msg}",
                "先修语法（本自检需要能解析源码才能检查纪律）。",
            )
        ]


def _literal_keys(node: ast.AST) -> list[tuple[str, int]]:
    """取出一个 dict 字面量的**字符串键**（含 `**{...}` 展开里的键）。"""
    keys: list[tuple[str, int]] = []
    if not isinstance(node, ast.Dict):
        return keys
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.append((key.value, key.lineno))
    return keys


def check_metric_labels() -> list[Problem]:
    """指标标签里不许出现高基数 / PII 的键（**AST 版**，不产生假阳性）。

    判据：一个 dict 字面量作为**标签参数**传给了 `X.counter(...)` / `X.observe(...)`。
    这样：
      * `state.py` 的 `{"user_id": ...}`（图状态）不受影响；
      * `ticket.py` 的 `insert(... {"user_id": ...})`（插入列）不受影响；
      * `langfuse_cb.py` 的 metadata dict 不受影响；
      * 只有真的写进指标标签的那些才会被拦下。
    """
    problems: list[Problem] = []
    for path in _python_files(APP):
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        tree, issues = _parse(path, relative)
        problems.extend(issues)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in METRIC_WRITE_METHODS:
                continue
            # 标签参数：第 2 个位置参数，或 `labels=` 关键字
            candidates: list[ast.AST] = []
            if len(node.args) >= 2:
                candidates.append(node.args[1])
            for keyword in node.keywords:
                if keyword.arg == "labels":
                    candidates.append(keyword.value)
            for candidate in candidates:
                for key, lineno in _literal_keys(candidate):
                    if key in FORBIDDEN_METRIC_LABELS:
                        problems.append(
                            Problem(
                                "指标标签",
                                f"{path.relative_to(ROOT)}:{lineno}",
                                f'标签键 {key!r} 被写进了指标（{name} 的第 2 个参数）',
                                "标签必须是**低基数**且不含 PII 的量（endpoint / status_class / "
                                "node / tool / model / outcome）。"
                                "需要按用户维度下钻时，用 trace 或日志（那里有 request_id），"
                                "**不要**加标签 —— 每个唯一标签值都是一条要存 15 天的时间序列。",
                            )
                        )
    return problems


def check_direct_span_calls() -> list[Problem]:
    """业务代码不许自己拼 span 名（只允许 `app/observability/` 内部调 `trace_span`）。

    为什么这条是硬纪律：`node.` / `tool.` / `sql.` / `llm.` / `retrieval.` 五个前缀
    决定了耗时进哪条指标（`tracing._duration_series` 是唯一的映射处）。
    调用点自己拼 `f"node.{name}"` 时，拼错一个字母**不会报错** ——
    只会让那条 span 的耗时**静默不进任何指标**（症状是「这个节点的 P99 怎么没有」）。
    所以业务代码只调 `instrument` 的具名入口（`node_span` / `embed_span` / `llm_span`
    / `record_sql_query` / `record_retrieval` / `record_tool_call` / `trace_request_span`）。
    """
    problems: list[Problem] = []
    for path in _python_files(APP):
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        # `app/observability/tracing.py` 是**实现层**（那些函数本身就在那里定义）；
        # `app/observability/instrument.py` 是**唯一的名字构造者**（它是允许的）；
        # 其余模块（含 observability 包里的别的文件）都必须走具名入口。
        if relative in {SPAN_NAME_OWNER, "app/observability/tracing.py"}:
            continue
        if relative in ALLOWED_DIRECT_SPAN_CALLERS:
            continue
        tree, issues = _parse(path, relative)
        problems.extend(issues)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            called = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if called in {"trace_span", "trace_llm", "trace_retrieval", "trace_sql", "trace_tool_call"}:
                problems.append(
                    Problem(
                        "span 命名",
                        f"{relative}:{node.lineno}",
                        f"这个模块直接调了 tracing.{called}(...) —— span 名的构造规约必须只有一处",
                        "改调 `app.observability.instrument` 的具名入口："
                        "节点 → `node_span(name)`；LLM → `llm_span(model)`；"
                        "embedding → `embed_span(...)`；SQL → `record_sql_query(...)`；"
                        "检索 → `record_retrieval(...)`；工具 → `record_tool_call(...)`；"
                        "请求根 → `trace_request_span(method, path)`。"
                        "（不要往 ALLOWED_DIRECT_SPAN_CALLERS 里加例外："
                        "那只是把一个「接口缺口」记成一条需要人遵守的纪律。）",
                    )
                )
    return problems


def check_span_attribute_whitelist() -> list[Problem]:
    """span 属性白名单必须存在，且**不含**明显会带 PII 的键。

    白名单本身写在 `tracing.ALLOWED_ATTRIBUTES`（**白名单而不是黑名单**：
    新增一个带 PII 的属性时默认不出网）。这条检查保证那个集合没有被顺手扩大。
    """
    problems: list[Problem] = []
    from app.observability.tracing import ALLOWED_ATTRIBUTES

    risky = {
        "prompt",
        "sql",
        "query",
        "params",
        "message",
        "messages",
        "answer",
        "content",
        "row_data",
        "rows_data",
    }
    leaked = risky & set(ALLOWED_ATTRIBUTES)
    if leaked:
        problems.append(
            Problem(
                "属性白名单",
                SPAN_ATTRIBUTE_WHITELIST,
                f"`ALLOWED_ATTRIBUTES` 里出现了可能带 PII / 会膨胀的键：{sorted(leaked)}",
                "span 属性会进 trace UI、日志与外部的 Langfuse。"
                "这类键要**移出**白名单（值本身留在日志/审计里，那里有访问控制）。",
            )
        )
    # 白名单里的 `tokens` / `rows` / `cost_usd` 这类**数字**键是安全的（不可能含 PII），
    # 所以这里只检查「名字看起来像文本内容」的那些。
    return problems


def check_required_series() -> list[Problem]:
    """文档 §2 点名的指标必须都在常量里（改名/删除会静默让看板失效）。"""
    problems: list[Problem] = []
    from app.observability import metrics as M

    required = {
        "REQUEST_COUNT": "shopsage_requests_total",
        "REQUEST_DURATION": "shopsage_request_duration_seconds",
        "AGENT_DURATION": "shopsage_agent_duration_seconds",
        "TOOL_DURATION": "shopsage_tool_duration_seconds",
        "SQL_DURATION": "shopsage_sql_duration_seconds",
        "LLM_DURATION": "shopsage_llm_duration_seconds",
        "LLM_TOKENS": "shopsage_llm_tokens_total",
        "LLM_COST": "shopsage_llm_cost_usd_total",
        "RETRIEVAL_DURATION": "shopsage_retrieval_duration_seconds",
        "SPAN_ERRORS": "shopsage_span_errors_total",
        "TOOL_OUTCOME": "shopsage_tool_outcome_total",
        "QUEUE_DEPTH": "shopsage_queue_depth",
    }
    for constant, expected in required.items():
        actual = getattr(M, constant, None)
        if actual != expected:
            problems.append(
                Problem(
                    "指标常量",
                    "app/observability/metrics.py",
                    f"{constant} 现在是 {actual!r}，期望 {expected!r}",
                    "指标名是**对外契约**（第 9/11 步的看板、记录规则、告警都按它写）。"
                    "改名要同步改 infra/prometheus/rules/ 与 Grafana 看板，"
                    "并跑 `uv run python -m scripts.check_infra`。",
                )
            )
    return problems


def main() -> int:
    checks = (
        ("① 指标标签基数与 PII", check_metric_labels),
        ("② span 名只在 instrument 里构造", check_direct_span_calls),
        ("③ span 属性白名单", check_span_attribute_whitelist),
        ("④ 文档点名的指标常量", check_required_series),
    )
    problems: list[Problem] = []
    for title, check in checks:
        found = check()
        print(f"{title}：{'❌ 发现 %d 个问题' % len(found) if found else '✅ 通过'}")
        problems.extend(found)

    print()
    if problems:
        print(f"共 {len(problems)} 个问题：")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("✅ 可观测性纪律自检通过（改了埋点/标签/span 名之后建议重跑本脚本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
