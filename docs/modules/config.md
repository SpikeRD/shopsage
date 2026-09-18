# config — 多环境配置与密钥管理

## 1. 职责

集中管理所有运行配置与密钥，让同一份代码能无改动地在「本机开发 / Linux 生产」之间切换。
只负责**读取和校验**配置，不负责业务逻辑。

## 2. 对外接口

```python
from app.config import get_settings          # 依赖注入用（FastAPI Depends）
from app.config import Settings              # Pydantic Settings 类

settings = get_settings()
settings.database_url      # 数据库连接串
settings.redis_url         # Redis 连接串
settings.llm_primary       # 主模型名
settings.llm_api_key       # 从环境变量读，永不硬编码
```

## 3. 依赖

- `pydantic-settings`（`BaseSettings`，从 `.env` / 环境变量加载）
- `python-dotenv`（加载 `.env`）
- 被几乎所有模块依赖（db / cache / queue / llm / observability 都读它）

## 4. 关键设计决策

1. **一份配置类 + 多环境 `.env`**：用 `ENV=dev|prod` 一个开关切换，而不是维护多份 config 文件（架构第 9 节「一致性」要求）。
2. **密钥不进 Git**：`.env` 加进 `.gitignore`，只提交 `.env.example`（占位符，无真实值）。
3. **启动即校验**：Pydantic `model_validator` 在启动时校验必填项，缺 key 直接 fail-fast，避免跑一半才发现。
4. **敏感值只读不打印**：Settings 覆写 `__repr__`，日志/异常里不泄露 API Key。

## 5. 核心技术点（要懂为什么）

- **`BaseSettings` 的加载优先级**（从高到低）：进程环境变量 > `.env` 文件 > 代码默认值。生产环境用环境变量覆盖 `.env`，正是「Linux 上走环境变量注入密钥」的依据。
- **为什么用 `lru_cache` 包 `get_settings`**：保证进程内只解析一次 `.env`，多次调用返回同一实例（单例），避免重复 IO 和配置漂移。
- **`SecretStr`**：把 API Key 声明为 `SecretStr`，打印时自动脱敏为 `**********`。

## 6. 开发步骤

1. 建 `app/config/__init__.py` + `settings.py`，定义 `Settings(BaseSettings)`。
2. 字段覆盖：`ENV`、`database_url`、`redis_url`、`llm_primary`、`llm_fallback`、`llm_api_key: SecretStr`、`otel_endpoint`、`langfuse_public_key` 等。
3. 加 `model_validator`：`ENV=prod` 时必须存在 `llm_api_key`、`database_url`，否则 raise。
4. 覆写 `__repr__` 隐藏敏感字段。
5. 写 `.env.example`（全部占位符）+ 把 `.env` 加入 `.gitignore`。
6. 写单测（见下）。

## 7. 测试用例

```python
def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    s = Settings()  # ENV 默认 dev
    assert s.database_url == "postgresql://u:p@localhost/db"

def test_prod_requires_llm_key(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()

def test_secret_not_in_repr(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-123")
    s = Settings()
    assert "sk-secret-123" not in repr(s)
```

## 8. 验收标准

- [ ] `.env` 不入 Git（`git status` 无 `.env`），`.env.example` 已提交。
- [ ] `ENV=prod` 缺失 `LLM_API_KEY` 时启动直接失败（fail-fast）。
- [ ] 上述 3 个测试全绿。
- [ ] 证据链接：`PR#__`
