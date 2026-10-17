# syntax=docker/dockerfile:1
#
# ShopSage 业务镜像（api / worker / migrate 三个角色共用同一份镜像）
#
# 构建（在【仓库根】执行，context 必须是仓库根）：
#   docker build -f app/Dockerfile -t shopsage-api:latest .
# 想连本地 embedding / rerank 后端一起装（+约 2 GB torch）：
#   docker build -f app/Dockerfile --build-arg WITH_LOCAL_EMBEDDING=true -t shopsage-api:latest .
#
# 三个刻意的决定：
#   ① 依赖安装走 `uv sync --locked`（与开发机同源）。`--locked` 在 uv.lock 与
#      pyproject.toml 不一致时**直接失败**，把「本地能跑、服务器上装的版本不同」挡在构建期；
#   ② 依赖层只由 pyproject.toml / uv.lock 决定 —— 改 .py 不会让这一层失效（重建几十秒）；
#   ③ 运行期**非 root**，且只有 HF 缓存目录可写（`app/llm/hf_cache.py` 会把 HF_HOME
#      指向 <仓库根>/.hf-cache，容器里就是 /app/.hf-cache）。

ARG PYTHON_VERSION=3.12
# ⚠️ 与 pyproject.toml 的 `required-version = ">=0.11"` 对齐。钉死版本而不是用 latest：
#    镜像构建要可复现，而 uv 的 lock 格式随版本变化（升级该显式改这一行）。
ARG UV_VERSION=0.11.33

# ── 阶段 0：uv 二进制（只有这一层依赖 UV_VERSION）──────────────────────────
# ⚠️⚠️ 为什么要有这个 stage：BuildKit **不支持 `COPY --from=` 里的变量展开**，
#     写成 `COPY --from=ghcr.io/astral-sh/uv:<版本> ...`（版本用 ARG 拼）会直接失败：
#       ERROR: failed to build: failed to solve:
#         variable expansion is not supported for --from,
#         define a new stage with FROM using ARG from global scope as a workaround
#     报错本身就给了修法：**在一处使用 `FROM` 的 stage 里做展开**（`FROM` 支持 ARG），
#     再让后面的 `COPY --from=uv` 只引用**stage 名**。
#     （旧版 buildx 有时是「不报错、按字面字符串 pull 一个不存在的镜像」，更难看出来。）
#     ⚠️ 全局 ARG 在第一个 FROM 之前声明，才能被 FROM 引用 —— UV_VERSION 正好满足。
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ── 阶段 1：依赖层 ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS deps

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    # ⚠️ 禁止 uv 自己下载解释器：容器里已经有了，下载只会让构建多一次网络依赖，
    #    且可能拿到与基础镜像不同的补丁版本（与 2.infra.md §5「只钉 minor」冲突）。
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 只复制依赖清单：这两行不变 → 这一层永远命中缓存
COPY pyproject.toml uv.lock ./

# ⚠️ 三个参数一个都不能少：
#    --locked             lock 过期直接失败（护栏，见模块注释 ①）
#    --no-dev             生产不装 pytest / pypdf 这些 dev 组依赖
#    --no-install-project 本层只装第三方依赖，项目自身在下一阶段「代码就在 /app/app」，
#                         不需要被 install 成 site-packages（改代码不重建依赖层）
# ⚠️ `--extra local` 是**可选**的：sentence-transformers（含 torch）刻意不是默认依赖
#    （见 pyproject.toml 的注释）。不装它时 EMBEDDING_BACKEND=local / RAG_RERANK=local
#    会在**第一次检索**才报错（服务能起来、/health 全绿）—— 所以这是一个必须显式选的口径。
ARG WITH_LOCAL_EMBEDDING=false
RUN if [ "$WITH_LOCAL_EMBEDDING" = "true" ]; then \
        echo "→ 安装本地 embedding/rerank 依赖（--extra local，约 2 GB）"; \
        uv sync --locked --no-dev --no-install-project --extra local; \
    else \
        echo "→ 不安装本地 embedding/rerank 依赖（只用 API 后端）"; \
        uv sync --locked --no-dev --no-install-project; \
    fi

# ── 阶段 2：运行镜像 ────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# PYTHONUNBUFFERED：日志立刻出容器（否则 `docker compose logs` 会延迟/丢尾部）
# TZ：scripts/seed.py 用 ZoneInfo("Asia/Shanghai")，没有 zoneinfo 数据会直接抛异常
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    HOME=/app \
    PATH="/app/.venv/bin:$PATH"

# ca-certificates：调 LLM / Langfuse 的 HTTPS 全靠它（slim 镜像不保证有）
# tzdata：ZoneInfo("Asia/Shanghai") 的数据来源
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 shopsage \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin shopsage

WORKDIR /app

COPY --from=deps /app/.venv /app/.venv

# ⚠️ 只复制**运行时真正需要**的东西。特别注意两件事：
#    ① `alembic/` 与 `alembic.ini` 必须有：迁移是部署的一部分（migrate 角色靠它们）；
#    ② `scripts/` 必须有：只读角色初始化、灌语料、种子数据都在容器里跑
#       （`docker compose run --rm api python -m scripts.xxx`）。
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
# 知识库语料：`scripts.load_knowledge_base` 的默认 root 与 RagSettings.knowledge_dir 都是 docs/kb
COPY docs/kb ./docs/kb

# ⚠️ 只有这一个目录需要可写（HF 权重缓存），所以**只 chown 它**：
#    对整个 /app 做 chown -R 会把 .venv 里几万个文件逐个改主，慢且没有收益。
#    命名卷首次挂载时会继承这个目录的属主，所以 uid 10001 能直接写。
RUN mkdir -p /app/.hf-cache && chown -R shopsage:shopsage /app/.hf-cache

USER shopsage

EXPOSE 8000

# 默认角色是 api。worker / migrate 由 compose 用 `command:` 覆盖
# （同一个镜像三个角色，避免「镜像里的代码和跑的代码不是一份」）。
#
# ⚠️ 入口模块只能是 `app.main:app`：`app/api/main.py` 导出的是 `create_app` 工厂，
#    写成 `app.api.main:app` 会得到 `Attribute "app" not found in module "app.api.main"`。
# ⚠️ 这里**不跑迁移**：迁移由 compose 的 `migrate` 一次性服务负责（多副本同时
#    `alembic upgrade head` 会互相抢锁）。见 docker-compose.yml 的 migrate 服务。
#
# ⚠️⚠️ `--workers` **默认 1**，这是一个刻意偏保守的选择，理由是**指标正确性**：
#    uvicorn 的 --workers 是「一个进程 fork 出 N 个」，它们**共享同一个监听端口**；
#    而 prometheus_client 的注册表是**进程内**的。于是 N 个 worker 时：
#      · 每次 `GET /metrics` 由**随机一个** worker 应答；
#      · 结果 Prometheus 每次抓到的计数在 N 份之间跳变（它以为同一个 instance 在抖动）；
#      · 记录规则里的 `sum by (le, node)` 因此**静默低估**真实值。
#    要真上多副本，必须启用 prometheus_client 的 multiprocess 模式
#    （设 `PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`）—— 本仓未做，
#    所以这里不假装它已经好了。见 docs/runbook/17.docker-ops.md §6.6。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
