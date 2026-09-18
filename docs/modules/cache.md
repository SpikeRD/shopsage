# cache — 语义缓存 / 结果缓存 / 缓存失效

## 1. 职责

对「重复/相似问题」命中缓存直接返回，降低 LLM 调用量与 token 成本；同时保证「强时效数据」不被错误缓存。

## 2. 对外接口

```python
from app.cache.semantic import semantic_cache_get, semantic_cache_set
from app.cache.result import result_cache_get, result_cache_set
```

## 3. 依赖

- `redis`（缓存存储）
- embedding 模型（对问题做向量化，判断「相似」）+ `pgvector` 或 Redis 内相似度
- 读 `config` 的 `redis_url`
- 被 `agents_module.md` 的 RAG/Data Agent 在调用 LLM 前查缓存

## 4. 关键设计决策

1. **语义缓存而非「精确字符串缓存」**：用户问「怎么退货」和「退货流程是啥」字面不同、语义相同；对问题做 embedding，相似度超阈值即命中（E1）。
2. **强时效问题禁用缓存 / 短 TTL**：订单状态、库存、物流**必须实时查库**，这些问题打标 `no_cache` 或 TTL 极短（F1）。
3. **命中阈值保守**：阈值太高缓存没命中率，太低会答错问题（相似但其实是两个问题）；需要 golden set 调阈值（`eval.md`）。
4. **缓存值存「答案 + 溯源 + 时间戳」**：命中时连出处一起返回，保证可追溯。

## 5. 核心技术点（要懂为什么）

- **相似度 ≠ 等价**：两条问题 embedding 近，可能是「同义」也可能是「相关但答案不同」，所以阈值要保守 + 对答案做时效校验。
- **缓存失效三策略**：时间过期（TTL）、主动失效（数据变更时删 key）、版本失效（prompt/知识库版本变了全局失效）。
- **为什么缓存放在 LLM 调用之前**：命中则整条链路（检索→生成）都不走，省的不只是 token 还有延迟（B2 首 token）。

## 6. 开发步骤

1. 写 `semantic.py`：问题 embedding → 查相似 → 命中返回 / 未命中 set。
2. 写 `result.py`：普通结果缓存 + TTL。
3. 写失效策略：知识库/数据变更时主动删 key。
4. 给「订单/库存/物流」类意图打 `no_cache` 标。
5. 写命中率统计，导出到 `observability.md` 的指标。

## 7. 测试用例

```python
def test_similar_question_hits_cache():
    semantic_cache_set("怎么退货", answer)
    assert semantic_cache_get("退货流程是什么") == answer

def test_dissimilar_question_misses():
    assert semantic_cache_get("怎么改地址") is None   # 与退货无关，不命中

def test_time_sensitive_not_cached():
    # 订单/库存类问题不走语义缓存（no_cache）

def test_ttl_expires():
    # 超过 TTL 后不再命中，重新生成

def test_invalidate_on_change():
    # 知识库更新后，旧缓存被主动失效
```

## 8. 验收标准

- [ ] 语义缓存命中率 X%，LLM 成本下降 Y%（E1，用 `observability.md` 数据支撑）。
- [ ] 强时效数据（订单/库存/物流）不被缓存或短 TTL（F1）。
- [ ] 首 token 延迟下降（配合 WebSocket 流式，B2）。
- [ ] 证据链接：`PR#__` / `成本报表#__`
