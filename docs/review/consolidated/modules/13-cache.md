# 模块 13 · cache（语义缓存 / 结果缓存 / 三级失效）

## 目的

同一句话（或换个说法）不再重复调用 LLM；但**强时效数据（订单/库存）绝不能给过期答案**。三件事：查（判定）、存（三套实现 + 降级）、失效（三级）。

## 关键结论

### 13.1 判定要过五道闸（顺序即语义）

`app/cache/core.py:326 judge()`：

1. **scope**（谁问的）+ **版本指纹**（`corpus_revision` + embedding 模型 + 检索参数）必须一致；
2. **时效分档**（policy / time_sensitive / default）决定 TTL；
3. **相似度兜底线**：低于 `semantic_floor` → 未命中早退；
4. **相似度命中线**：`[floor, threshold)` 是**灰区** —— 判未命中，但把候选与分数带进账目（可观测"像但不敢答"）；
5. **多轮会话**：默认不命中（`last_multi_turn`）。

阈值不是拍脑袋定的：

| 参数 | 当前值 | 依据 |
|---|---|---|
| `CACHE_SIM_THRESHOLD` | **0.85** | qwen3.7-text-embedding-flash 实测：同义命中 4/5、误命中 0/2（`app/cache/settings.py:57-92`） |
| `CACHE_SIM_FLOOR` | **0.70** | 换后端后"相关但答案不同"落在 0.68~0.78，兜底线必须卡在它与"不相干"(0.35~0.46) 之间（`app/cache/settings.py:94-107`） |
| `CACHE_TTL_SECONDS` | 24h | `app/cache/settings.py:110` |
| policy 分档 TTL | 7 天 | `:112` |
| time_sensitive TTL | 60s | `:115` |

⚠️ **换 embedding 模型后必须重新标定这两个数**：沿用 bge-m3 时代的 0.93 会让同义命中掉到 1/5（`app/cache/settings.py:86-91` 的实测表）。

### 13.2 存储：三套实现 + 一场降级棋

- 快路径 Redis（键值层）+ 慢路径 pgvector（语义层）+ 内存后端（兜底）。四格降级矩阵**每一格都可用**（`docs/runbook/13.cache-runbook.md` §4.1）。
- **降级必须可见**：`CacheHealth`（`app/cache/api.py:64`）+ `/health` 的 `cache` 组件（`app/api/health.py:197`）。静默降级等于"缓存悄悄失效，成本悄悄上涨"。
- **跨机器时钟**：时间戳对齐数据库那一边（不是应用机）。

### 13.3 三级失效

| 级 | 机制 | 代码 |
|---|---|---|
| 1 | TTL 到期 | `app/cache/settings.py:172 ttl_for()` |
| 2 | 主动失效 | `invalidate_document()` / `invalidate_scope()`（`app/cache/invalidate.py:39/65`） |
| 3 | 版本失效 | `corpus_revision` 变化 → 指纹全变（`app/cache/corpus.py:219 bump_after_ingest()`） |

### 13.4 两个必须记住的"接线"教训（都是真踩过的）

- **`issues/009`**：缓存命中时 `rag_node` 返回 `docs: []`，输出护栏看不到材料 → 把**缓存里的真答案**判成编造。修法是 `app/agents/guardrail.py:337-340` 把 `cache_info` / `cached_answer_used` 补进材料摘要。
- **`issues/010`**：检索回 0 条的答案被写进缓存且灌库不推进版本 → 知识库修好了，缓存还在说"没找到"。修法两处：灌库推进 `corpus_revision`（`app/rag/ingest.py` 补 `doc_changed`）、**空结果不进缓存**（`app/agents/synthesis.py`，产品已确认）。

## 实际用法

```python
# app/cache/__init__.py:119/124
prepared = lookup(messages, user_id=2, role="customer", params={"top_k": 5})
if prepared.hit:
    answer = prepared.answer          # 直接用
else:
    ...                               # 正常跑图

remember(messages, answer, ...)       # 写回（空结果不写）
```

## 待办 / 已知边界

- ⚠️ 缓存命中率、成本下降幅度这些**量化指标**没有稳定的历史记录（`docs/problem-solving.md` E1 的"证据链接"仍是 `PR#__`）。→ `../../03-待确认清单.md` Q-06。
- `CACHE_SIM_THRESHOLD` 的标定只覆盖"退货"这一个问题域；其他域（物流、保修）的分布未单独测 —— 迁移时同样需要重跑 `scripts/cache_demo.py --scenario threshold`。

## 来源

- `docs/modules/13.cache.md`（47 行）、`docs/runbook/13.cache-runbook.md`（1332 行）
- `issues/009`、`issues/010`、`docs/problem-solving.md` E1/F1
- 代码：`app/cache/*.py`（14 文件）；测试：`tests/cache/*.py`（11 文件）
