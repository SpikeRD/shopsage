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
    # ── 开发依赖栈 ──────────────────────────────────────────────────────────
    "infra/docker-compose.dev.yml",
    "infra/prometheus/prometheus.yml",
    "infra/prometheus/rules/shopsage.yml",
    "infra/otel/otel-collector.yml",
    "infra/grafana/provisioning/datasources/prometheus.yml",
    "infra/grafana/provisioning/dashboards/shopsage.yml",
    # ── 生产全栈（第 17 步 runbook 的落地物）─────────────────────────────────
    # 加进来不只是「多解析一个文件」：`docker compose config -q` 只能在 Linux 上跑，
    # 而开发机（Windows）没有 Docker —— 于是生产 compose 的 YAML 语法错误在开发期
    # **没有任何东西会拦住它**，只能等到部署那一刻才发现。这个脚本补上那道闸。
    "docker-compose.yml",
    "infra/prometheus/prometheus.prod.yml",
)
DASHBOARD = "infra/grafana/provisioning/dashboards/shopsage-observability.json"

#: 需要做「scrape 目标自洽性」检查的 Prometheus 配置文件。
#: ⚠️ 两份都要查：dev 的 target 是写死的 Windows 网卡 IP，prod 的 target 是服务名 `api:8000`。
#: 只查一份的后果是「另一份写错了没人知道，直到那套环境里 target 全 down」。
PROMETHEUS_FILES = (
    "infra/prometheus/prometheus.yml",
    "infra/prometheus/prometheus.prod.yml",
)

#: 允许出现在记录规则表达式里的指标名前缀（我们这个项目自己的指标）。
METRIC_RE = re.compile(r"\b(shopsage[a-z0-9_]*)\b")

#: 任何 `${VAR}` / `$VAR` 形式出现在 scrape 目标地址里 —— 一律判错。
#: 实测结论（踩过两次）：prom/prometheus:v2.55.1 **不会**替换 static_configs 里的环境变量
#: （`expand-external-labels` 在 2.x 只作用于 external_labels，3.0 移除该 flag 后才默认生效）。
#: 两种写法的症状都是「配置合法、日志干净、target 却永久 down」：
#:   `${VAR:-1.2.3.4:8000}` → invalid port ":8000%7D"
#:   `${VAR}`               → 展开成空主机名，http://:80/metrics → invalid URL escape "%7B"
_ENV_REF_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*")


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
    """Prometheus 的 scrape 目标与录则挂载必须自洽（dev 与 prod 两份都查）。

    为什么两份都要查：它们的 target 口径**故意不同**（dev 是写死的 Windows 网卡 IP，
    prod 是服务名 `api:8000`），而写错的后果一致 —— 不报错、只是 target 永久 down。
    只查一份时，另一份的错误要等到那套环境部署后才发现。
    """
    problems: list[str] = []
    for relative in PROMETHEUS_FILES:
        problems += _check_one_prometheus(relative)
    return problems


def _check_one_prometheus(relative: str) -> list[str]:
    problems: list[str] = []
    label = Path(relative).name
    data, error = _load_yaml(relative)
    if error or not data:
        return [error or f"{label} 为空"]

    jobs = {job.get("job_name") for job in data.get("scrape_configs") or []}
    print(f"  [{label}] scrape jobs：{sorted(jobs)}")
    for required in ("prometheus", "shopsage-api"):
        if required not in jobs:
            problems.append(f"{relative} 缺少 job：{required}（第 11 步的验收要求业务被采集）")

    api_job = next((j for j in data.get("scrape_configs") or [] if j.get("job_name") == "shopsage-api"), None)
    if api_job and api_job.get("metrics_path") != "/metrics":
        # 这是最容易写错的一处：`/metrics` **不在** API_PREFIX 下（见 app/api/health.py 的口径）
        problems.append(
            f"{relative} 的 shopsage-api metrics_path 是 {api_job.get('metrics_path')!r}，应当是 '/metrics'"
            "（`/metrics` 刻意不在 API_PREFIX 下，否则改前缀会让抓取端集体失效）"
        )

    rule_files = data.get("rule_files") or []
    print(f"  [{label}] rule_files：{rule_files}")
    if not rule_files:
        problems.append(f"{relative} 没有 rule_files：分 Agent 的 P99 记录规则不会被加载")

    # rule_files 指向容器内路径；仓库侧必须真实存在对应目录（否则 compose 挂载不到）
    rules_dir = ROOT / "infra" / "prometheus" / "rules"
    if not rules_dir.exists():
        problems.append("infra/prometheus/rules/ 不存在：rule_files 指向的目录挂不进容器")

    for job in data.get("scrape_configs") or []:
        for config in job.get("static_configs") or []:
            for target in config.get("targets") or []:
                # 实测踩过两次：`static_configs.targets` 里写 `${...}` 时，
                # prom/prometheus:v2.55.1 **不做替换也不报错**，而是把字面量当主机名
                # （或展开成空主机名）→ target 永久 down、日志干净、config -q 也通过。
                # 所以这类写法一律判错：地址必须写死（可配置的话请换文件或升级 3.x）。
                if _ENV_REF_RE.search(str(target)):
                    problems.append(
                        f"{relative} 的 job {job.get('job_name')} target 是 {target!r}，里面含环境变量引用 —— "
                        "Prometheus 2.x 不替换 static_configs 里的 `${...}`（只作用于 external_labels，"
                        "3.0 才默认生效），它既不报错也不替换：target 会永久 down"
                        "（`invalid URL escape \"%7B\"` 或 `invalid port \":8000%7D\"`）。"
                        "请把地址写死，例如 targets: [\"192.168.1.13:8000\"]（Windows 网卡 IP，"
                        "地址取法见 11.observability-runbook.md §6.2）；"
                        "需要可配置就在部署期生成这个文件，或升级 Prometheus 3.x"
                    )
                if "localhost" in str(target) and job.get("job_name") != "prometheus":
                    problems.append(
                        f"{relative} 的 job {job.get('job_name')} target 是 {target!r}："
                        "Prometheus 在容器里，localhost 指的是它自己（业务在宿主机上）"
                    )

    # 生产那份**必须**用服务名而不是 IP：全栈容器化后业务就叫 `api`
    if label.endswith(".prod.yml"):
        targets = [
            str(target)
            for job in data.get("scrape_configs") or []
            if job.get("job_name") == "shopsage-api"
            for config in job.get("static_configs") or []
            for target in config.get("targets") or []
        ]
        if targets and not any(t.startswith("api:") for t in targets):
            problems.append(
                f"{relative} 的 shopsage-api target 是 {targets}：生产全栈容器化后业务在同栈的 "
                "`api` 容器里，应当写服务名 `api:8000`（拿开发那份写死 IP 的配置来跑生产，"
                "target 会永久 down 且日志干净）"
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


def check_prod_compose() -> list[str]:
    """生产 compose 的**结构**检查（第 17 步 runbook 落地物）。

    `docker compose config -q` 只能在 Linux 上跑（开发机 Windows 没有 Docker），
    所以下面这些都只能在部署那一刻才被发现 —— 而每一条的失败形态都很绕：

    | 检查 | 不做会怎样 |
    |---|---|
    | 每个业务服务都有 `build` 或 `image` | `up -d` 直接报 `has neither an image nor a build context` |
    | `api`/`worker` 都 `depends_on: migrate: service_completed_successfully` | api 抢在迁移前面起来 → 表是旧的（`UndefinedColumn` 500） |
    | 生产 compose 里没有 `${POSTGRES_*}` 这类业务变量插值 | 插值读仓库根 `.env`，与 `env.list` 两处真相必然漂移 |
    | `prometheus` 挂的是 `prometheus.prod.yml` | 拿开发那份（target 是 Windows IP）跑生产 → target 永久 down |
    """
    problems: list[str] = []
    data, error = _load_yaml("docker-compose.yml")
    if error or not data:
        return [error or "docker-compose.yml 为空"]
    services = data.get("services") or {}
    print(f"  服务：{sorted(services)}")

    for name, service in services.items():
        if not isinstance(service, dict):
            problems.append(f"docker-compose.yml 的 {name} 不是映射")
            continue
        if "build" not in service and "image" not in service:
            problems.append(
                f"docker-compose.yml 的 {name} 既没有 image 也没有 build："
                "`up -d` 会报 `has neither an image nor a build context`"
            )

    for name in ("api", "worker"):
        depends = (services.get(name) or {}).get("depends_on") or {}
        migrate_dep = depends.get("migrate") if isinstance(depends, dict) else None
        condition = (migrate_dep or {}).get("condition") if isinstance(migrate_dep, dict) else None
        if condition != "service_completed_successfully":
            problems.append(
                f"docker-compose.yml 的 {name} 没有 "
                "`depends_on: migrate: condition: service_completed_successfully`："
                "它会在迁移跑完之前启动，表结构可能是旧的（症状是 UndefinedColumn 500）"
            )

    # 业务变量不该在 compose 里插值（插值读 .env，与 env.list 是两套来源）
    forbidden = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "DATABASE_URL", "REDIS_URL")
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    leaked = [name for name in forbidden if f"${{{name}" in text]
    if leaked:
        problems.append(
            f"docker-compose.yml 里有业务变量的插值：{leaked} —— "
            "插值读的是仓库根 `.env`，而容器读 `env.list`：两处真相必然漂移。"
            "把它们只留在 env.list（需要覆盖时用 `environment:` 直接写值）"
        )

    # 生产 Prometheus 必须挂 prod 那份配置
    prom_volumes = (services.get("prometheus") or {}).get("volumes") or []
    prom_sources = [str(v).split(":")[0] for v in prom_volumes if isinstance(v, str)]
    if not any(src.endswith("prometheus.prod.yml") for src in prom_sources):
        problems.append(
            f"docker-compose.yml 的 prometheus 挂的不是 prometheus.prod.yml（实际：{prom_sources}）—— "
            "开发那份的 target 是 Windows 网卡 IP，在生产全栈容器里必然永久 down"
        )

    # env_file 必须指向 env.list（而不是 .env）
    api_env_file = (services.get("api") or {}).get("env_file")
    env_files = [api_env_file] if isinstance(api_env_file, str) else list(api_env_file or [])
    if "env.list" not in env_files:
        problems.append(
            f"docker-compose.yml 的 api 没有 `env_file: env.list`（实际：{env_files}）—— "
            "容器里的业务配置将全部退回代码默认值"
        )
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
    print("⑤ 生产 compose 结构：")
    problems += check_prod_compose()
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
