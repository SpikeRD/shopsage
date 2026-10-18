# 模块 05 · llm（LLM 接入层）

## 目的

把"调模型"收成两个函数（`llm_call` / `embed`），并统一**错误语义**：调用方只需要判断"稍后重试"还是"别再试"。

## 关键结论

1. **门面只有两个入口**：`app/llm/client.py:381 llm_call()`、`:464 embed()`。底层是 LiteLLM（`pyproject.toml:33 litellm>=1.50`）。
2. **重试已落地，降级与熔断未落地**：
   - `_RetryPolicy`（`app/llm/client.py:98`）用 tenacity 做**指数退避 + jitter**，`MAX_BACKOFF_SECONDS=20`、`DEFAULT_MAX_RETRIES=3`、`DEFAULT_TIMEOUT=30`（`:49-53`）；
   - ❌ **没有自动 fallback 链**：`llm_call` 签名里刻意没有 `fallback_models`（`app/llm/client.py:9-10` 自述"属第 13 步"）；
   - ❌ **没有熔断器**（全仓 `circuit`/`breaker` 零命中）。
   - 后果：`docs/problem-solving.md` 的 A3（多模型降级）实际**未完成** —— 见 `../../02-异常清单.md` E-03。
3. **错误只有两个语义**：`RetryableLLMError` / `PermanentLLMError`（`app/llm/errors.py:56-99`）。分类依据是异常类型集合 `RETRYABLE`（429/超时/5xx）与 `NON_RETRYABLE`（4xx 参数错误）—— **4xx 参数错误绝不重试**。
4. **密钥按模型解析**：`_resolve_credentials()`（`app/llm/client.py:225`）—— 模型名 == `llm_fallback` 时用 `llm_fallback_api_key` + `llm_fallback_api_base`；未配则**明确报错**而不是偷偷用主密钥（prod 侧 `app/config/settings.py:445-450` 也会拒绝启动）。
5. **embedding 双后端**，同一个签名：
   - `EMBEDDING_BACKEND=litellm`（默认）→ 走 API，当前配置 `dashscope/qwen3.7-text-embedding-flash`（`env.list.example:116-118`）；
   - `EMBEDDING_BACKEND=local` → sentence-transformers（`uv sync --extra local`，会拉 torch ≈ 2GB；代码里**函数内 import**，否则默认环境连 `import app.llm` 都失败，见 `pyproject.toml:54-61`）。
   - **两个后端的向量空间不兼容**：换后端必须重建索引 + 重灌向量（`app/rag/store.py:254-262` 有维度自检）。
6. **维度校验**：`_assert_embedding_dim()`（`app/llm/client.py:355`）在写入前比对 `EMBEDDING_DIM` / 表定义维度，避免"表 vector(1024) 写入 8 维"这类错。

## 实际用法

```python
# app/llm/client.py:381 （节选语义）
async def llm_call(messages, *, model=None, **kwargs) -> LLMResponse:
    """返回 LLMResponse(content, tokens, latency_ms, model...)；错误抛 Retryable/PermanentLLMError。"""

# app/llm/client.py:464
async def embed(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """按 EMBEDDING_BACKEND 分派到 API 或本地模型。"""
```

```python
# app/llm/client.py:98 —— 重试策略（指数退避 + jitter）
class _RetryPolicy(AsyncRetrying):
    ...
```

启动时的失败面（`tests/test_llm_client_retry.py` 钉住）：
- 429 → 退避重试，最后仍失败 → `RetryableLLMError`；
- 400/422（参数错）→ **一次都不重试** → `PermanentLLMError`；
- 超时 → 重试。

## 待办 / 已知边界

- **多模型自动降级 + 熔断器未实现**（A3 未完成）。要补的话，`errors.is_retryable()` 已经是输入信号（`docs/modules/5.llm.md:49`）。
- ⚠️ "第 13 步"指代冲突（一处指 llm 加固、一处指 cache），见 `../../02-异常清单.md` E-12。
- 本地 embedding 后端在容器里需要 `WITH_LOCAL_EMBEDDING=true` 且 `EMBEDDING_BACKEND=local` 同时成立，否则镜像里没有 sentence-transformers —— 服务照常起、`/health` 全绿，**第一次检索才报错**（`docker-compose.yml:28-32` 注释）。

## 坑

| 坑 | 依据 |
|---|---|
| `litellm.__version__` 不存在（AttributeError）→ 版本要从包元数据读 | `docs/runbook/5.llm-runbook.md` §1.1 |
| `openai` 必须**显式**声明依赖（即使 litellm 会带进来） | `pyproject.toml:35` |
| 降级模型复用主密钥 → 跨厂商必 401，而 401 不可重试 | `app/config/settings.py:441-450` |
| 换 embedding 后不重建索引 → 语义检索全废 | `app/rag/store.py:254-262` |

## 来源

- `docs/modules/5.llm.md`（209 行）、`docs/runbook/5.llm-runbook.md`（1325 行）
- 代码：`app/llm/client.py`（457 行）、`errors.py`、`hf_cache.py`；`tests/test_llm_client_retry.py`、`test_llm_embed*.py`
