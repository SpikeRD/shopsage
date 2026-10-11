"""集中管理运行配置与密钥：多环境切换 + 启动即校验。

只负责「读取和校验」配置，不参与业务逻辑。
加载优先级（高 → 低）：进程环境变量 > .env 文件 > 字段默认值。
"""

import logging
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_serializer, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 不添加任何 handler：日志交给应用层（app/config/logging.py）配置，
# 避免库代码擅自决定日志输出位置。
# 注意：Python 的 lastResort handler 会把 WARNING 及以上打到 stderr，
# 所以下面那条冲突告警在未配置日志时也是可见的 —— 这正是 fail-loud 的本意。
logger = logging.getLogger(__name__)

# 敏感字段：只读不打印，repr / 日志里整体脱敏为 **********
_SENSITIVE_FIELDS = frozenset({"llm_api_key", "langfuse_secret_key", "jwt_secret"})

# 可能内嵌密码的连接串：整体掩码会丢掉排障价值，所以只掩码密码段
_MASKED_URL_FIELDS = ("database_url", "database_read_url", "redis_url")

# 匹配 scheme://user:password@ 中的密码段。
# 密码段用贪婪的 [^/\s]*，这样密码里出现未转义的 @ 也能被一并吃掉，
# 不会只掩码前半段而把后半段漏出去。
_URL_PASSWORD_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)"
    r"(?P<user>[^:/@\s]*):(?P<password>[^/\s]*)@"
)


def _mask_url_password(value: str) -> str:
    """只把连接串里的密码段换成 ***，保留 host/port/db 以便排障。

    例：postgresql://u:p@localhost/db → postgresql://u:***@localhost/db
    """
    return _URL_PASSWORD_RE.sub(r"\g<scheme>\g<user>:***@", value)


def _read_dotenv_values(env_file: object, encoding: str) -> dict[str, str] | None:
    """读取 .env 的原始键值（兜底方案，见 _dotenv_field_values）。

    返回 None 表示「没有可读的 .env 文件」。
    """
    if isinstance(env_file, (str, Path)):
        env_files: tuple[str | Path, ...] = (env_file,)
    elif isinstance(env_file, (tuple, list)):
        env_files = tuple(env_file)
    elif env_file is None:
        return None
    else:
        # 非预期的 env_file 形态：不与 pydantic 争抢解释权，直接放弃检测。
        return None

    merged: dict[str, str] = {}
    for item in env_files:
        if not isinstance(item, (str, Path)):
            continue
        try:
            for key, value in dotenv_values(Path(item).expanduser(), encoding=encoding).items():
                if value is not None:
                    # 后读到的覆盖先读到的，与 pydantic-settings 的顺序语义一致
                    merged[key] = value
        except OSError:
            continue
    return merged or None


def _dotenv_field_values(settings_cls: type[BaseSettings]) -> Mapping[str, object]:
    """取出「.env 实际提供的字段值」，键是字段名（不是环境变量名）。

    首选路径：调用 pydantic-settings 自己解析好的 DotEnvSettingsSource —— 键映射
    （别名 / 前缀 / 大小写）由它负责，我们不重复实现，否则就成了「用手写规则猜
    pydantic 的映射」，迟早对不上。

    兜底路径：source 不可用时直接读 .env 原文。这条路径下键是**环境变量名**
    （如 DATABASE_URL），所以调用方查表时要自己处理映射 —— 这就是为什么它只是兜底。
    """
    source = getattr(settings_cls, "_dotenv_source", None)
    if callable(source):
        try:
            resolved = source()
        except Exception:  # noqa: BLE001 - 诊断功能绝不阻断启动
            resolved = None
        if isinstance(resolved, dict):
            return resolved

    config = settings_cls.model_config
    raw = _read_dotenv_values(
        config.get("env_file"),
        str(config.get("env_file_encoding") or "utf-8"),
    )
    return raw or {}


def _warn_on_env_collisions(
    field_values: Mapping[str, object],
    dotenv: Mapping[str, object],
    env: Mapping[str, str],
    env_prefix: str,
    case_sensitive: bool,
) -> None:
    """检测「同名进程环境变量正在覆盖 .env 里的值」，命中就告警。

    背景：加载优先级是 env > .env，所以 Windows 用户级环境变量（`setx` 设的、
    在「系统属性 → 环境变量」里配的）会**静默**盖掉项目 .env 的同名项。表现是
    「我明明在 .env 里改了，配置却没变」，极难排查。这里把静默变成显式告警，
    从而不需要为了避坑去清空或迁就 OS 环境变量。

    只在「两边都存在且值不同」时告警 —— 值相同时谁覆盖谁都无所谓，不制造噪音。

    参数：`dotenv` 用字段名作键（即 DotEnvSettingsSource 的解析结果），不用
    原始环境变量名 —— 键映射（别名 / 前缀 / 大小写）交给 pydantic-settings 自己做，
    这里不重复实现，否则就成了「用手写规则猜 pydantic 的映射」。
    """
    os_env = {(k if case_sensitive else k.upper()): v for k, v in env.items()}

    for name, value in field_values.items():
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            continue  # 非字符串字段无法与 .env 原文可靠对账

        file_value = dotenv.get(name)
        if not isinstance(file_value, str):
            continue  # .env 里没有这一项 → 不存在覆盖关系

        key = f"{env_prefix}{name}"
        if not case_sensitive:
            key = key.upper()
        os_value = os_env.get(key)
        if os_value is None:
            continue  # 进程环境变量里没有 → 不存在覆盖关系
        if file_value == os_value:
            continue  # 值相同 → 覆盖无害

        # 值不同：报告冲突，但绝不打印任何一个值。
        # 连接串只露 host/db（排障要的正是这个），密钥类整体掩码。
        if name in _MASKED_URL_FIELDS:
            shown = _mask_url_password(raw)
        elif name in _SENSITIVE_FIELDS:
            shown = "**********"
        else:
            shown = raw

        logger.warning(
            "配置冲突：进程环境变量 %s 正在覆盖 .env 中的同名值（生效值: %s）。"
            "优先级为「环境变量 > .env」，如需以 .env 为准请改环境变量名或调整其值。",
            key,
            shown,
        )


class Settings(BaseSettings):
    """全项目唯一配置类。

    用 `ENV=dev|prod` 一个开关切换环境，而不是维护多份 config 文件。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # 配置在启动时定型，运行期不得改写：frozen 会拒绝字段赋值与「静默新增属性」
        # （`settings.env = "prod"` / `settings.typo = 1` 都抛 ValidationError）。
        # 注意它拦的是「手滑改字段」这种改名式越权；model_copy(update=...) 仍可用，
        # 那是「造一个改过的副本」的正规姿势，不该一起堵掉。
        frozen=True,
        # extra="allow"：接收 .env 里「本组件不认识的键」（其他模块的配置项），
        # 避免它被 pydantic 直接报错。注意 .env 里的键不会进进程环境变量，
        # 所以允许 extra 不等于放宽了配置来源。
        extra="allow",
        # 关键：校验失败时不要把「输入原值」回显进 ValidationError。
        # 否则下面那个 after-validator 抛错时，pydantic 会把整个注入 dict
        # （含 llm_api_key 原文）写进报错文本 → 生产启动失败的日志里泄露密钥。
        hide_input_in_errors=True,
    )

    # 环境开关
    env: Literal["dev", "prod"] = "dev"

    # 数据 / 缓存
    database_url: str = ""
    # 只读连接串（第 4 步 security 落地）：Text-to-SQL 的读路径只走它，写操作在数据库层被拒。
    # 留空 = 读路径回落到 `database_url`（此时「只读」只在命名上成立，见 3.db-runbook §5.3）。
    # ⚠️ 必须**显式声明**成字段，不能靠 extra="allow" 兜：pydantic 的 extra 只保留
    #    「看起来像本模型字段」的键，第 1 步就定好的 `DATABASE_READ_URL` 属于配置的一部分。
    database_read_url: str = ""
    redis_url: str = ""

    # 鉴权（第 4 步 security）：JWT 签名密钥。
    # 用 SecretStr，保证 repr / dump_for_logging / 报错回显里都不会出现明文（同 llm_api_key 的待遇）。
    # 生产（ENV=prod）缺失时 `app/security/auth.py` 会拒绝启动 —— 不允许静默用弱默认密钥。
    jwt_secret: SecretStr = SecretStr("")

    # LLM（多模型路由：主 + 备）
    # ⚠️ 密钥按用途分层：主模型 / 降级模型 / embedding 各一把，**不共用**。
    #    理由见 docs/runbook/5.llm-runbook.md §1.2.1：降级链天生跨厂商（主 DeepSeek、备 OpenAI
    #    之类），拿 A 家的 key 打 B 家的端点必然 401，而降级发生的时刻正是主模型已经不可用的时候，
    #    此刻失败＝直接对用户报错。此外还带来故障隔离（主 key 被限流时备路径仍可用）、独立轮换、账单可归属。
    llm_primary: str = ""
    llm_fallback: str = ""
    llm_api_key: SecretStr = SecretStr("")
    # 降级模型**专用**密钥（留空 = 没配，此时用 fallback 模型会明确报错，见 _resolve_credentials）
    llm_fallback_api_key: SecretStr = SecretStr("")
    # 降级模型的端点覆盖（仅当备模型走 OpenAI 兼容网关/自建代理时需要）
    llm_fallback_api_base: str = ""

    # 向量化（第 5 步 llm 的 embed() 用；第 10 步 rag 检索、第 13 步 cache 判相似都走它）。
    # 与 llm_primary 分开配置：embedding 端点常常不是同一个厂商/同一个模型族。
    # 支持两种后端（接口完全一样，调用方不感知）：
    #   litellm → 调 API：按 token 计费、文本出网、可能 429/超时（可重试）
    #   local   → 本地 sentence-transformers：零边际成本、文本不出网、吃本机 CPU/内存
    #             需要可选依赖 `uv sync --extra local`；选型判据见 runbook §7.2
    embedding_backend: Literal["litellm", "local"] = "litellm"
    embedding_model: str = "openai/text-embedding-3-small"
    # embedding 单独密钥；留空 = 复用 llm_api_key（**厂商与主模型不同时必须单独填**）
    embedding_api_key: SecretStr = SecretStr("")
    # 期望向量维度：>0 时实际维度不符**直接报错**（0 = 不校验）。
    # 防的是「换了模型/后端后维度变了」——最坏情况不是维度不一致（写库会响），
    # 而是**维度恰好相同但向量空间不相容**：检索看着正常、结果全是乱码，掉分时你会先去怀疑 prompt。
    embedding_dim: int = Field(default=0, ge=0)
    # 仅 local 后端：离线环境的本地模型目录；device 写 cpu 最稳（避免无 CUDA 机器上刷 warning）
    embedding_local_path: str = ""
    embedding_device: str = ""

    # ── RAG（第 10 步 rag）：检索层的配置面 ────────────────────────────────────
    # ⚠️ 这三个字段（backend / model / dim）**必须与向量表的 `vector(n)` 一致**，
    #    顺序是先定它们、再写迁移（`10.rag.md` 第 3 节）。改任何一项都要重建索引 + 重灌向量。
    #
    # 三个数值必须成套修改，所以在这里放一条启动期校验（_validate_rag_vector_space）：
    # 维度不一致最坏的形态不是写库报错，而是「维度恰好相同但向量空间不相容」。
    #
    # 表维度：0 = 不校验（只用于「只想跑不碰库的单测」这类场景）；>0 时与实际建表维度比对。
    rag_vector_dim: int = Field(default=0, ge=0)
    # 混合检索的初召回条数（rerank 的输入池）。太小 → rerank 无料可选；太大 → rerank 变慢（E2）。
    rag_candidates: int = Field(default=30, ge=1, le=200)
    # RRF 融合常数 k（`1/(k + rank)`）。60 是 Cormack 等人的经验值：k 越大越"平权"两路召回。
    rag_rrf_k: int = Field(default=60, ge=1, le=1000)
    # 关键词腿的覆盖率门槛：命中 2-gram / 已知 2-gram 的比例（见 app/rag/textkit.py）。
    # 定得太低 → 关键词腿变成"只要一个字相同就召回"（低精度）；太高 → 少打一个字就召回不到。
    rag_keyword_min_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
    # 关键词腿的 BM25 参数（k1 控 tf 饱和、b 控长度归一化；1.2/0.75 是经典默认值）。
    rag_bm25_k1: float = Field(default=1.2, gt=0.0)
    rag_bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)
    # rerank 后端：off = 关掉（离线/评测对照）；local = 本地 CrossEncoder；api = 兼容 /rerank 的 HTTP 端点。
    # 默认 local：本仓 .env 的 embedding 也走本地（文本不出网），rerank 与它口径一致才谈得上"零边际成本"。
    rag_rerank: Literal["off", "local", "api"] = "local"
    rag_rerank_model: str = "BAAI/bge-reranker-base"
    rag_rerank_api_base: str = ""
    rag_rerank_api_key: SecretStr = SecretStr("")
    # 单次检索最多取回多少条（对客 top-k 的上限；LLM 要 "Top 500" 时由这里兜底，见 E2）
    rag_max_top_k: int = Field(default=20, ge=1, le=200)
    # 分块参数：max_chars 是硬上限（超长必切），overlap 是相邻块的重叠字符数（防止答案被切在缝上）。
    rag_chunk_max_chars: int = Field(default=600, ge=80, le=8000)
    rag_chunk_overlap: int = Field(default=80, ge=0, le=2000)

    # ── 可观测（第 11 步 observability）：三件套的配置面 ────────────────────────
    #
    # 三个开关的设计口径是**「不配 = 不打扰业务」**：
    #   * otel_endpoint 留空 → **只把 span 留在进程内存里**（开发机没有 collector），
    #     配了才走 OTLP/HTTP 外发。这一条很重要：留空也去建 exporter 的话，
    #     每个请求都会在后台重试连接并超时（症状是「服务越跑越慢」，而原因在监控上）。
    #   * Langfuse 的密钥留空 → **完全不构造 client**（实测：给了假密钥它会真的建 TLS 连接
    #     并在后台重试导出 401；而 `public_key=""` 只是打一条 WARNING 把自己禁用）。
    #   * observability_enabled=False → 所有埋点退化成 no-op（压测对照 / 排障时「怀疑监控拖慢」）。
    observability_enabled: bool = True
    otel_endpoint: str = ""
    # 服务名：进 OTEL 的 resource 与日志，多服务共用一个 collector 时靠它区分
    service_name: str = "shopsage-api"
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    # 自建 Langfuse 的地址（留空 = SDK 默认，即 Langfuse Cloud）
    langfuse_host: str = ""
    # Langfuse 总开关（**即使两个密钥都配好了也能关掉它**）。
    #
    # 为什么需要这个开关（而不是「密钥就是开关」）：
    # Langfuse SDK 会注册一个 `atexit` 钩子，进程退出时它去做一次 flush ——
    # 实测（端点不可达）：最短 2.0s、最长 23.6s；**记过 score 之后 6~13s**
    # （score 走 SDK 内部另一条无上限的队列，不受 `timeout` 参数约束）。
    # 于是「有关键路径的密钥、但这次不想上报」（压测对照、离线演示、临时排障）
    # 缺一个**不影响密钥配置**的出口 —— 否则只能去改密钥（改了会连带影响别的用途）。
    langfuse_enabled: bool = True

    # 实例化时由 settings_customise_sources 重绑的「实际生效的 dotenv 来源」。
    # 只服务冲突检测这一个诊断用途。
    #
    # 为什么要存这个而不是直接读 model_config["env_file"]：`Settings(_env_file=...)`
    # 是**按实例**传的，pydantic 实例化后不保留该属性，model_config 里永远只有类
    # 默认值 ".env"，于是 per-instance 覆盖会漏检。DotEnvSettingsSource 手里才拿着
    # 真正解析过的路径与值。
    _dotenv_source: ClassVar[object] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: object,
        env_settings: object,
        dotenv_settings: object,
        file_secret_settings: object,
    ) -> tuple[object, ...]:
        """钩住来源装配，只记录 dotenv 来源供冲突检测对账。

        这是 pydantic-settings 的公开扩展点。返回顺序即优先级顺序 ——
        保持父类默认的「init > 环境变量 > .env > secrets 目录」不变。
        """
        cls._dotenv_source = dotenv_settings
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _validate_llm_routing(self) -> "Settings":
        """LLM 路由字段的形状校验。

        这里**刻意不**校验模型名是否被 LLM 厂商认识 —— 那等于替 litellm 维护一张
        它自己的模型表，属于典型的过度约束。只拦两种必然出错的形状：
          1. 主模型为空 → 调用必然失败，且发生在请求路径上；
          2. 备模型与主模型相同 → 「降级」变成再打一次同样的请求，静默丧失可用性收益。
        """
        primary = self.llm_primary.strip()
        if not primary:
            raise ValueError(
                "llm_primary 不能为空（LLM_PRIMARY 被显式设为空/空白）。"
                "留空不会回退到默认模型，请填写模型名。"
            )
        if primary != self.llm_primary:
            object.__setattr__(self, "llm_primary", primary)

        # embedding_model 与 llm_primary 同类：空/空白会让 embed() 在请求路径上必然失败，
        # 且 litellm 对空模型名的报错很隐晦（会走到「不认识这个 provider」），所以在这里拦住。
        embedding = self.embedding_model.strip()
        if not embedding:
            raise ValueError(
                "embedding_model 不能为空（EMBEDDING_MODEL 被显式设为空/空白）。"
                "留空不会回退到默认模型，请填写模型名。"
            )
        if embedding != self.embedding_model:
            object.__setattr__(self, "embedding_model", embedding)

        fallback = self.llm_fallback.strip()
        if fallback != self.llm_fallback:
            object.__setattr__(self, "llm_fallback", fallback)
        if fallback and fallback == primary:
            raise ValueError(
                "llm_fallback 不能与 llm_primary 相同：降级会变成重复打同一个模型，"
                "既拿不到可用性收益，还会让故障时的重试量翻倍。"
            )

        # 密钥分层的一致性检查（见 docs/runbook/5.llm-runbook.md §1.2.1）：
        #  ① 配了 fallback 却**没有**它自己的 key → 降级会以 401 结束，且 401 属不可恢复错误，
        #     既不会重试也不会再降级，可用性比不加降级还差。dev 只告警（开发时常留空 fallback），
        #     prod 由 _validate_prod_requirements 直接拒绝启动 —— 宁可开不了机，也不要故障时才暴露。
        #  ② 两把 key **相同** → 不是错误（同厂商两个模型确实可能共用一把），但意味着**没有故障隔离**，
        #     值得在启动日志里说一句。
        fallback_key = self.llm_fallback_api_key.get_secret_value()
        primary_key = self.llm_api_key.get_secret_value()
        if fallback and not fallback_key:
            logger.warning(
                "配置了 llm_fallback=%s 但没有 LLM_FALLBACK_API_KEY："
                "降级时会拿主模型的密钥去打备用端点，跨厂商必然 401，"
                "而降级正是主模型不可用的时候。请补上备用模型的独立密钥。",
                fallback,
            )
        elif fallback_key and fallback_key == primary_key:
            logger.warning(
                "LLM_FALLBACK_API_KEY 与 LLM_API_KEY 相同：可以工作，但没有故障隔离 ——"
                "主密钥被限流/超额时，备用路径会一起失效。"
                "若两个模型属于不同厂商，请换成各自的密钥。"
            )

        return self

    @model_validator(mode="after")
    def _warn_on_env_collisions(self, info: object) -> "Settings":
        """把「OS 环境变量静默覆盖 .env」变成启动期显式告警（见模块级函数注释）。"""
        config = type(self).model_config
        dotenv = _dotenv_field_values(type(self))

        # extra="allow" 时 pydantic 只保留「看起来像本模型字段」的额外键，
        # 所以这里拿不到、也不需要对 .env 里其他模块的键做检测。
        extra = getattr(info, "data", None) or {}
        field_values: dict[str, object] = dict(self.__dict__)
        field_values.update(extra)

        _warn_on_env_collisions(
            field_values=field_values,
            dotenv=dotenv,
            env=os.environ,
            env_prefix=str(config.get("env_prefix") or ""),
            case_sensitive=bool(config.get("case_sensitive")),
        )
        return self

    @model_validator(mode="after")
    def _validate_prod_requirements(self) -> "Settings":
        """启动即校验：生产环境缺关键配置直接 fail-fast，避免跑一半才发现。"""
        if self.env != "prod":
            return self

        missing: list[str] = []
        if not self.database_url:
            missing.append("database_url")
        if not self.llm_api_key.get_secret_value():
            missing.append("llm_api_key")
        if missing:
            raise ValueError(
                f"ENV=prod 缺少必填配置: {', '.join(missing)}"
                "（请通过环境变量或 .env 注入）"
            )

        # 配了降级模型却没有它自己的密钥：prod **拒绝启动**。
        # 理由：降级链跨厂商时拿主密钥去打备用端点必然 401，而 401 属不可恢复错误，
        # 既不会重试也不会再降级 —— 可用性反而比「干脆不配降级」更差。
        # 这条不能只告警：prod 的告警没人看，故障时才暴露（见 runbook §1.2.1）。
        if self.llm_fallback and not self.llm_fallback_api_key.get_secret_value():
            raise ValueError(
                f"ENV=prod 配置了 llm_fallback={self.llm_fallback}，"
                "但缺少 llm_fallback_api_key（LLM_FALLBACK_API_KEY）："
                "降级模型的密钥必须独立配置，不能复用 llm_api_key。"
            )

        # 不 fail-fast：降级链也可能由代码内置（见 llm.md 第 13 步），所以只告警。
        # 注意这条日志**没有** %-插值参数，所以文本里的百分号必须写成单个 %，
        # 否则 logging 不做插值、字面 "%%" 会直接印出来。
        if not self.llm_fallback:
            logger.warning(
                "ENV=prod 未配置 llm_fallback：主模型故障/限流时无降级链可用，"
                "可用性目标（架构 A3: 99.9%）缺少兜底。"
                "若降级链由代码内置可忽略本告警。"
            )
        return self

    # --- 信息隔离：把「显示出口」和「跨边界出口」全部纳管 ---
    #
    # 出口分三类，处置方式不同：
    #   ① 显示出口   repr / str / f-string      → 必须掩码
    #   ② 跨边界出口 model_dump_json()          → 必须掩码（进日志/HTTP/Langfuse）
    #   ③ 功能出口   settings.database_url 属性 → 必须是真值，否则连不上库
    #
    # 注意 pydantic 的 BaseModel.__str__ 不会调用我们覆写的 __repr__，
    # 它自己拼字段显示；所以 __str__ 必须单独覆写，否则 str(settings) 会泄露明文。

    @field_serializer("database_url", "database_read_url", "redis_url", when_used="json")
    def _serialize_masked_urls(self, value: str) -> str:
        """连接串走 JSON 序列化时掩码密码段（json 出口通常意味着跨进程/进日志）。"""
        return _mask_url_password(value)

    def dump_for_logging(self) -> dict[str, Any]:
        """打印/上报配置时用这个，别用 model_dump()。

        两者功能上都是「取值」，区别在**意图表达**：
          - `model_dump()`（python 模式）返回**真值**，用途是「拿连接串去连库」，
            顺手打进日志就是一次全量泄露（连接串密码 + API Key）；
          - 本方法返回**已掩码**值，用途就是「打印看配置」。

        实现上直接复用 JSON 出口，所以两处掩码规则不可能漂移；有测试钉住这一点。
        """
        return dict(self.model_dump(mode="json"))

    def __repr__(self) -> str:
        """覆写 __repr__：日志/异常里不泄露任何密钥，含连接串里的密码。

        只影响「显示」——字段自身的值不变，数据库连接照常可用。
        """
        data = self.model_dump()
        for key in _SENSITIVE_FIELDS:
            if key in data:
                data[key] = "**********"
        for key in _MASKED_URL_FIELDS:
            value = data.get(key)
            if isinstance(value, str):
                data[key] = _mask_url_password(value)
        return f"{type(self).__name__}({data!r})"

    def __str__(self) -> str:
        """与 __repr__ 保持一致，堵住 str() / f-string 这条独立出口。"""
        return self.__repr__()


@lru_cache
def get_settings() -> Settings:
    """进程内单例：只解析一次 .env，多次调用返回同一实例，避免重复 IO 和配置漂移。"""
    return Settings()
