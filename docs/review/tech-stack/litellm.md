# LiteLLM + tenacity（LLM 接入与重试）

## 本项目用它解决什么

用**一套接口**打多家模型（当前 `LLM_PRIMARY=deepseek/deepseek-flash`、embedding 走 `dashscope/qwen3.7-text-embedding-flash`），
并把"重试多少、什么时候别重试"收成两条明确的错误语义。

## 实际用法（代码片段 + 文件:行号）

### 1）对外只有两个函数

```python
# app/llm/client.py:381 —— 聊天
async def llm_call(messages, *, model=None, **kwargs) -> LLMResponse: ...

# app/llm/client.py:464 —— 向量化（按 EMBEDDING_BACKEND 分派 API / 本地）
async def embed(texts: list[str], *, model: str | None = None) -> list[list[float]]: ...
```

### 2）返回值是自有结构（为了"计费口径只有一处"）

```python
# app/llm/client.py:117-136
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None
    latency_ms: int
    finish_reason: str = ""   # 区分"模型没说话"与"被 max_tokens 截断"
```

### 3）重试：指数退避 + jitter，只重试可恢复错误

```python
# app/llm/client.py:49-53
MAX_BACKOFF_SECONDS = 20.0
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3

# app/llm/client.py:98
class _RetryPolicy(AsyncRetrying): ...

# app/llm/client.py:139-150 —— 异常翻译（分两类）
def _translate(exc: Exception, *, max_retries: int) -> LLMError:
    if is_permanent(exc):
        return PermanentLLMError(...)
    return RetryableLLMError(f"LLM 重试 {max_retries} 次后仍失败：...")
```

```python
# app/llm/errors.py:26-56
RETRYABLE: tuple[type[BaseException], ...] = (...)       # 429 / 超时 / 5xx
NON_RETRYABLE: tuple[type[BaseException], ...] = (...)   # 4xx 参数错误
class LLMError(RuntimeError): ...
class RetryableLLMError(LLMError): ...
class PermanentLLMError(LLMError): ...
```

### 4）密钥按模型解析（降级模型必须独立密钥）

```python
# app/llm/client.py:225-248（节选）
def _resolve_credentials(model: str, settings: Any) -> tuple[str, str | None]:
    fallback_key = settings.llm_fallback_api_key.get_secret_value()
    if settings.llm_fallback and model == settings.llm_fallback:
        if not fallback_key:
            raise PermanentLLMError("…降级模型必须用独立密钥：跨厂商复用主密钥必然 401…")
        return fallback_key, (settings.llm_fallback_api_base or None)
    ...
```

### 5）embedding 双后端，同一个签名

```python
# app/llm/client.py:271-324（节选）
_local_model = None                       # 进程级缓存 + 锁
def _load_local_model(model_name, *, device=""): ...    # 函数内 import sentence_transformers
async def _embed_local(items, model_name, settings): ...
def _validate_local_backend(model_name, settings): ...  # 未装 extras 时给出可执行的报错
```

## 最小原理

1. **LiteLLM 是"模型名的翻译层"**：`provider/model` → 对应厂商 SDK 的调用，统一返回 OpenAI 形状的 `ModelResponse`。所以换厂商只改配置。
2. **异步重试 = 包一层循环**：tenacity 的 `AsyncRetrying` 在协程外再套一个 `while`，每次 `await` 调用；退避时间由策略函数算（指数 + jitter 防"重试风暴"）。
3. **"可重试"由异常类型判定，不由字符串判定**：429/超时/5xx 是"服务器让我等"，400/422 是"我发错了"——后者重试只会更慢更贵。
4. **embedding 两个后端是两条不同的路**：API 后端按 token 计费、走网络；本地后端是 CPU/GPU 推理。两者**向量空间不兼容**（同维度也未必可比），换后端必须重建索引。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| `litellm.__version__` 不存在 | `AttributeError`；版本要从包元数据读 | `docs/runbook/5.llm-runbook.md` §1.1 |
| 没显式声明 `openai` | 靠 litellm 带进来的依赖迟早漂移 | `pyproject.toml:35` 显式声明 |
| 降级模型复用主密钥 | 跨厂商必 401，而 401 **不可重试** → 比不配降级更差 | `app/config/settings.py:441-450` |
| 以为有自动 fallback | `llm_call` 签名里**刻意没有** `fallback_models` | `app/llm/client.py:9-10` |
| 把 `max_tokens` 给小了 | 推理模型把 token 花在 reasoning 上 → `content` 为空但调用"成功" | `app/llm/client.py:133-136`（`finish_reason`） |
| 换 embedding 后端不重建索引 | 检索全错但**看起来正常** | `app/rag/store.py:254-262`、`app/cache/settings.py:86-91` |
| 生产镜像装 torch | 默认 `WITH_LOCAL_EMBEDDING=false`；两者必须一起改 | `docker-compose.yml:28-32` |

## 深入关键词

`litellm.acompletion` · `ModelResponse.usage` · `tenacity.AsyncRetrying` · `wait_exponential_jitter` · `RetryCallState` · `429 Too Many Requests` · `httpx.TimeoutException` · `sentence-transformers` · `HF_HOME` · `cosine similarity` · 向量维度一致性
