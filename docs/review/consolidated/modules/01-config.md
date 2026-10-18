# 模块 01 · config（配置与密钥管理）

## 目的

全项目**唯一**的配置类，把"环境变量 > `.env` > 代码默认值"这条优先级、密钥掩码、prod 启动自检三件事收在一处。

## 关键结论

1. **唯一配置类**：`app/config/settings.py:184` 的 `class Settings(BaseSettings)`，`frozen=True`（运行期改字段会抛 `ValidationError`）、`extra="allow"`（收下别的模块的键）、`hide_input_in_errors=True`（**报错不回显原值**，否则启动失败日志里会泄露 API Key）。—— `app/config/settings.py:190-207`
2. **密钥一律用 `SecretStr`**：`jwt_secret` / `llm_api_key` / `llm_fallback_api_key` / `embedding_api_key` / `rag_rerank_api_key` / `langfuse_secret_key`（`app/config/settings.py:224-281`，共 34 个字段）。`repr()` 与 `dump_for_logging()`（`:478`）都拿不到明文。
3. **prod 启动即失败**（`app/config/settings.py:424-461`）：
   - 缺 `database_url` 或 `llm_api_key` → `ValueError` 拒绝启动；
   - 配了 `llm_fallback` 却没配 `llm_fallback_api_key` → 拒绝启动（跨厂商复用主密钥必然 401）；
   - 缺 `llm_fallback` → 只 `warning`（降级链也可能由代码内置）。
4. **JWT 另有独立自检**：`app/security/auth.py:265` 的 `ensure_usable()`，在 `app/api/main.py:197` 的 lifespan 里调用 —— prod 用开发默认密钥或密钥 < 16 字符 → 启动失败。
5. **"表面字段"陷阱（最容易踩）**：`API_*` 这一组**不是** `Settings` 的字段，而由 `app/api/config.py:184` 的 `_env_value()` 先读 `os.environ`、再 `getattr(settings, ...)`、最后默认值。原因见 `app/api/config.py:194-199`：pydantic-settings 不把任意额外键放进模型，所以只走 `getattr` 会**静默用默认值**。

## 实际用法

```python
# app/config/settings.py:190 （节选）
model_config = SettingsConfigDict(
    env_file=ENV_FILE_NAME, env_file_encoding="utf-8", case_sensitive=False,
    frozen=True, extra="allow", hide_input_in_errors=True,
)
env: Literal["dev", "prod"] = "dev"
database_url: str = ""
database_read_url: str = ""      # 只读 DSN：Text-to-SQL 读路径只走它
jwt_secret: SecretStr = SecretStr("")
```

```python
# app/api/config.py:203-214 （API_* 的读取口径）
from_env = os.environ.get(field.upper())
if from_env is not None and str(from_env).strip() != "":
    return from_env
if settings is None:
    settings = get_settings()   # 读不到就退回默认值（不阻断起服务）
return getattr(settings, field, default)
```

```python
# app/api/config.py:43  业务路由前缀默认值
DEFAULT_PREFIX: Final[str] = "/api"
```

## 待办 / 已知边界

- `API_*` 系列（`API_PREFIX`、`API_CORS_ORIGINS`、`API_DEBUG_PAYLOAD`、`API_PROBE_TIMEOUT`、`RATE_LIMIT_*`）**不在 `Settings` 里**，`settings.dump_for_logging()` 看不到它们。要查真值请调 `app.api.config` 对应的函数（如 `api_prefix()` / `cors_origins()`）。
- `CHECKPOINT_BACKEND` 默认 **`memory`**（`app/api/config.py:178`），即"进程重启即丢会话"；生产必须显式写 `postgres`（`env.list.example:75` 已写）。
- 配置项分散在**五处**：`app/config/settings.py`（34 字段）、`app/api/config.py`（`API_*`）、`app/mcp/settings.py`、`app/eval/settings.py`、`app/cache/settings.py`。需要完整键名时按字段名 grep。

## 坑（来自 `docs/runbook/1.config-guide.md`）

| 坑 | 症状 | 依据 |
|---|---|---|
| `.env` 是**当前工作目录相对**的 | 在子目录跑命令 → 配置全默认值，**不报错** | `app/config/settings.py:29` 的 `ENV_FILE_NAME = ".env"` |
| 只走 `getattr(settings, field)` | 设了 `API_DEBUG_PAYLOAD=1` 行为仍是默认 | `app/api/config.py:194-199` |
| 以为 `extra="allow"` 能把 `.env` 的键塞进模型 | 同上 | 同上注释 |
| 改了 `API_PREFIX` 以为 `/health` 也变 | `/health`、`/metrics` 固定挂根 | `app/api/main.py:368`（`include_router(infra_router)` 不带 prefix） |

## 来源

- `docs/modules/1.config.md`（224 行，2026-10-14 20:38）
- `docs/runbook/1.config-guide.md`（342 行，2026-10-17 12:03）
- 代码：`app/config/settings.py`、`app/config/__init__.py`、`app/api/config.py`
- 测试：`tests/test_config.py`（576 行）
