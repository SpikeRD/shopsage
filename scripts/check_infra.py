"""基础设施配置的自检（开发期用；不进测试套件）。

做两件在「改 YAML 时最容易忘、后果又最隐蔽」的检查：

1. **YAML 能不能解析**（`docker-compose.dev.yml` / prometheus / rules / otel / grafana provisioning）
   —— compose 的一处缩进错误不会让 `up -d` 报错，只会**静默少起一个服务**；
2. **看板 JSON 里的 datasource uid、记录规则名、指标名是否与代码/配置对得上**
   —— 这三类错全部是「不报错、只是没数据」：
   * 看板 uid 对不上 → 所有面板 "Datasource not found"；
   * 记录规则引用了不存在的指标 → 规则**存在但永远无值**（图上是一条平线）；
   * `rule_files` 目录没挂 → Prometheus **启动失败**（这一条会报错，放在这里一并检查）。

用法（仓库根）：
    uv run python -m scripts.check_infra
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

YAML_FILES = (
    "infra/docker-compose.dev.yml",
    "infra/prometheus/prometheus.yml",
    "infra/prometheus/rules/shopsage.yml",
    "infra/otel/otel-collector.yml",
    "infra/grafana/provisioning/datasources/prometheus.yml",
    "infra/grafana/provisioning/dashboards/shopsage.yml",
)
DASHBOARD = "infra/grafana/provisioning/dashboards/shopsage-observability.json"

#: 允许出现在记录规则表达式里的指标名前缀（我们这个项目自己的指标）。
METRIC_RE = re.compile(r"\b(shopsage[a-z0-9_]*)\b")


def _load_yaml(relative: str) -> tuple[dict | None, str]:
    path = ROOT / relative
    if not path.exists():
        return None, f"缺少文件：{relative}"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), ""
    except yaml.YAMLError as exc:
        return None, f"YAML 解析失败（{relative}）：{exc}"


def check_yaml() -> list[str]:
    problems: list[str] = []
    for relative in YAML_FILES:
        data, error = _load_yaml(relative)
        if error:
            problems.append(error)
            continue
        print(f"  OK  {relative}（顶层键：{sorted(data) if isinstance(data, dict) else type(data).__name__}）")
    return problems


def check_prometheus_scrape() -> list[str]:
    """`prometheus.yml` 的 scrape 目标与录则挂载必须自洽。"""
    problems: list[str] = []
    data, error = _load_yaml("infra/prometheus/prometheus.yml")
    if error or not data:
        return [error or "prometheus.yml 为空"]

    jobs = {job.get("job_name") for job in data.get("scrape_configs") or []}
    print(f"  scrape jobs：{sorted(jobs)}")
    for required in ("prometheus", "shopsage-api"):
        if required not in jobs:
            problems.append(f"prometheus.yml 缺少 job：{required}（第 11 步的验收要求业务被采集）")

    api_job = next((j for j in data.get("scrape_configs") or [] if j.get("job_name") == "shopsage-api"), None)
    if api_job and api_job.get("metrics_path") != "/metrics":
        # 这是最容易写错的一处：`/metrics` **不在** API_PREFIX 下（见 app/api/health.py 的口径）
        problems.append(
            f"shopsage-api 的 metrics_path 是 {api_job.get('metrics_path')!r}，应当是 '/metrics'"
            "（`/metrics` 刻意不在 API_PREFIX 下，否则改前缀会让抓取端集体失效）"
        )

    rule_files = data.get("rule_files") or []
    print(f"  rule_files：{rule_files}")
    if not rule_files:
        problems.append("prometheus.yml 没有 rule_files：分 Agent 的 P99 记录规则不会被加载")

    # rule_files 指向容器内路径；仓库侧必须真实存在对应目录（否则 compose 挂载不到）
    rules_dir = ROOT / "infra" / "prometheus" / "rules"
    if not rules_dir.exists():
        problems.append("infra/prometheus/rules/ 不存在：rule_files 指向的目录挂不进容器")

    for job in data.get("scrape_configs") or []:
        for config in job.get("static_configs") or []:
            for target in config.get("targets") or []:
                if "localhost" in str(target) and job.get("job_name") != "prometheus":
                    problems.append(
                        f"job {job.get('job_name')} 的 target 是 {target!r}："
                        "Prometheus 在容器里，localhost 指的是它自己（业务在宿主机上）"
                    )
    return problems


def check_recording_rules() -> list[str]:
    """记录规则：表达式里的指标名必须是我们真的会产生的那些。"""
    problems: list[str] = []
    data, error = _load_yaml("infra/prometheus/rules/shopsage.yml")
    if error or not data:
        return [error or "rules 文件为空"]

    from app.observability import metrics as M

    known = {
        M.REQUEST_COUNT,
        M.REQUEST_DURATION,
        M.AGENT_DURATION,
        M.TOOL_DURATION,
        M.TOOL_OUTCOME,
        M.LLM_DURATION,
        M.SQL_DURATION,
        M.RETRIEVAL_DURATION,
        M.LLM_TOKENS,
        M.LLM_COST,
        M.SPAN_ERRORS,
        M.TRACE_COUNT,
        M.QUEUE_DEPTH,
    }
    # 第 9 步的对外契约名（`shopsage_api_*`）也必须在允许列表里：
    # 黄金指标（QPS/错误率/HTTP P99）是从它们算出来的。
    from app.api.metrics import PREFIX as API_PREFIX

    known |= {
        f"{API_PREFIX}_requests_total",
        f"{API_PREFIX}_request_duration_seconds",
        f"{API_PREFIX}_chat_turns_total",
        f"{API_PREFIX}_chat_turn_seconds",
        f"{API_PREFIX}_chat_route_total",
        f"{API_PREFIX}_stream_first_frame_seconds",
        f"{API_PREFIX}_stream_answer_seconds",
    }

    group_count = 0
    rule_count = 0
    for group in data.get("groups") or []:
        group_count += 1
        for rule in group.get("rules") or []:
            rule_count += 1
            expr = str(rule.get("expr") or "")
            referenced = set(METRIC_RE.findall(expr))
            # 记录规则自身产出的名字（`shopsage:xxx`）也可能被别的规则引用，加进允许集
            referenced = {name for name in referenced if not name.startswith("shopsage_")}
            unknown = {
                name
                for name in METRIC_RE.findall(expr)
                if name.startswith("shopsage_")
                and name not in known
                # `shopsage_agent_duration_seconds_bucket` 是直方图的分桶序列
                and not any(name.startswith(f"{base}_") for base in known)
            }
            if unknown:
                problems.append(
                    f"记录规则 {rule.get('record')!r} 引用了未知指标：{sorted(unknown)}"
                    "（指标名写错不会报错，只会让这条规则永远没有值 —— 图上是一条平线）"
                )
            del referenced
    print(f"  记录规则：{group_count} 组 / {rule_count} 条")
    if rule_count == 0:
        problems.append("rules 文件里一条规则都没有")
    return problems


def check_dashboard() -> list[str]:
    """看板的 datasource uid 必须与 provisioning 的数据源 uid 一致。"""
    problems: list[str] = []
    path = ROOT / DASHBOARD
    if not path.exists():
        return [f"缺少看板：{DASHBOARD}"]
    dashboard = json.loads(path.read_text(encoding="utf-8"))

    ds, error = _load_yaml("infra/grafana/provisioning/datasources/prometheus.yml")
    if error or not ds:
        return [error or "数据源配置为空"]
    declared = {item.get("uid") for item in ds.get("datasources") or []}
    print(f"  数据源 uid：{sorted(declared)}")

    def walk(node: object) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            if node.get("type") == "prometheus" and "uid" in node:
                found.append(str(node["uid"]))
            for value in node.values():
                found.extend(walk(value))
        elif isinstance(node, list):
            for value in node:
                found.extend(walk(value))
        return found

    used = set(walk(dashboard))
    missing = used - declared
    if missing:
        problems.append(
            f"看板引用了未声明的数据源 uid：{sorted(missing)}"
            f"（已声明：{sorted(declared)}）——症状是**所有面板显示 Datasource not found**，"
            "数据源却在列表里。provisioned 看板不会自动改写引用（手点导入才会）"
        )

    # 看板里引用的指标名也要能对上（同上：错了只是空图）
    text = path.read_text(encoding="utf-8")
    unknown = {
        name
        for name in METRIC_RE.findall(text)
        if name.startswith("shopsage_") and not name.startswith("shopsage_queue_depth")
        and name not in {
            "shopsage_api_requests_total",
            "shopsage_api_request_duration_seconds",
            "shopsage_agent_duration_seconds",
            "shopsage_agent_duration_seconds_count",
        }
    }
    if unknown:
        problems.append(f"看板引用了未知指标：{sorted(unknown)}")

    print(f"  看板：{dashboard.get('title')!r}，{len(dashboard.get('panels') or [])} 个面板")
    return problems


def main() -> int:
    print("① YAML 可解析性：")
    problems = check_yaml()
    print("② Prometheus 采集配置：")
    problems += check_prometheus_scrape()
    print("③ 记录规则与指标名对账：")
    problems += check_recording_rules()
    print("④ Grafana 看板与数据源对账：")
    problems += check_dashboard()
    print()
    if problems:
        print(f"❌ 发现 {len(problems)} 个问题：")
        for index, problem in enumerate(problems, 1):
            print(f"  {index}. {problem}")
        return 1
    print("✅ 基础设施配置自检通过（改 YAML 后建议重跑本脚本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
