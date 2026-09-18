"""集中管理运行配置与密钥：多环境切换 + 启动即校验。

只负责「读取和校验」配置，不参与业务逻辑。
加载优先级（高 → 低）：进程环境变量 > .env 文件 > 字段默认值。
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 敏感字段：只读不打印，repr / 日志里一律脱敏为 **********
_SENSITIVE_FIELDS = frozenset({"llm_api_key", "langfuse_secret_key"})


class Settings(BaseSettings):
    """全项目唯一配置类。

    用 `ENV=dev|prod` 一个开关切换环境，而不是维护多份 config 文件。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 环境开关
    env: Literal["dev", "prod"] = "dev"

    # 数据 / 缓存
    database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"

    # LLM（多模型路由：主 + 备）
    llm_primary: str = "gpt-4o-mini"
    llm_fallback: str = ""
    llm_api_key: SecretStr = SecretStr("")

    # 可观测
    otel_endpoint: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")

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
        return self

    def __repr__(self) -> str:
        """覆写 __repr__：敏感字段脱敏，日志/异常里不泄露 API Key。"""
        data = self.model_dump()
        for key in _SENSITIVE_FIELDS:
            if key in data:
                data[key] = "**********"
        return f"{type(self).__name__}({data!r})"


@lru_cache
def get_settings() -> Settings:
    """进程内单例：只解析一次 .env，多次调用返回同一实例，避免重复 IO 和配置漂移。"""
    return Settings()
