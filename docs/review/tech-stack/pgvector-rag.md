# PostgreSQL + pgvector + 自研 RAG（检索与向量）

## 本项目用它解决什么

语义召回（向量）+ 精确词召回（n-gram/BM25）双路混合，用 RRF 融合，可选 rerank。全部落在同一个 PostgreSQL 实例上，不引入独立向量数据库。

## 实际用法（代码片段 + 文件:行号）

### 1）向量腿：余弦距离 + HNSW 索引

```python
# app/rag/store.py:541-545
def search_vector(self, embedding: Sequence[float], *, top_k: int) -> list[VectorHit]:
    """向量腿：pgvector 的余弦距离 ANN（走 HNSW 索引）。

    排序用距离升序（等价于相似度降序），这样 `order by` 能直接吃 HNSW 索引 ——
    """
```

### 2）关键词腿：**n-gram 词表 + tsvector + SQL 内手算 BM25**

```python
# app/rag/store.py:9-13（模块 docstring）
# 1. **关键词腿是「n-gram 词表 + tsvector + SQL 内手算 BM25」**，不是 `ts_rank`。
# 自己算 BM25 需要三个量：tf（从 tsvector 的 nentry 里取）、df（按词逐个 `@@` 数，…
```

### 3）融合：RRF

```python
# app/rag/retriever.py:159
def reciprocal_rank_fusion(...) -> list[...]: ...
# 参数：RAG_RRF_K=60（app/config/settings.py:269）、RAG_CANDIDATES=30（:267）
```

### 4）维度一致性自检（本仓实测踩过）

```python
# app/rag/store.py:254-262
# 维度先自检（本仓实测踩到）：表是 `vector(1024)` 时写 8 维向量，…
raise StoreError("请把 EMBEDDING_DIM / RAG_VECTOR_DIM 与向量表的 vector(n) 统一（三处一起改）…")
```

### 5）迁移里的两条选型结论

```python
# alembic/versions/20261010_a1f0c3d5b7e9_add_rag_vector_tables.py:8-19（注释）
# 1. 维度 vector(1024) 是写死的一个数，必须与 EMBEDDING_DIM / RAG_VECTOR_DIM 一致
# 2. HNSW 而不是 IVFFlat：IVFFlat 要先有数据才能训练聚类中心，空表建索引会退化
# 4. 中文全文检索用 n-gram 词表 + tsvector：
#    to_tsvector('simple', 中文) 不切词；
#    to_tsvector 是 stable 不是 immutable，生成列会得到「有时对、有时错」的索引
```

### 6）语义缓存也压在 pgvector 上

```python
# app/cache/settings.py:92 / :107
DEFAULT_SIM_THRESHOLD: Final[float] = 0.85    # 命中线
DEFAULT_SIM_FLOOR: Final[float] = 0.70        # 兜底线（低于它早退）
```

## 最小原理

1. **pgvector 的 `<=>` 是余弦距离**（1 − cos），所以"距离升序 = 相似度降序"。
2. **HNSW 是近似最近邻图索引**：插入即可用，查询走图跳转；代价是内存与建索引时间。**IVFFlat 需要先有数据训练聚类中心**，空表建索引等于没建。
3. **BM25 的三个量**：`tf`（词在本片段的频次）、`df`（含该词的片段数）、`N/avgdl`（总片段数 / 平均长度）。`ts_rank` 不给这些，所以本仓在 SQL 里手算（`app/rag/store.py:442` 重算全局量）。
4. **中文为什么不能用 `to_tsvector('simple', ...)`**：PostgreSQL 的 simple 词典按空白切，中文整段会被当成一个词。所以先自建 n-gram 词表再喂 `tsvector`。
5. **`to_tsvector` 是 stable 不是 immutable**：生成列（`GENERATED ALWAYS AS`）要求表达式 immutable，用它建索引会"有时对有时错"。

## 常见坑

| 坑 | 症状 | 依据 |
|---|---|---|
| 维度三处不一致 | 首次写入向量时抛错 | `app/rag/store.py:254-262` |
| 用 IVFFlat 建空表索引 | 召回率极差 | 迁移文件 :12-13 |
| 用 `to_tsvector` 做生成列 | 索引结果不稳定 | 迁移文件 :16-19 |
| 换 embedding 后端不重建索引 | 检索全错但"看起来正常" | `app/rag/store.py:167` |
| 换 embedding 后不重标缓存阈值 | 同义命中从 4/5 掉到 1/5（实测） | `app/cache/settings.py:86-91` |
| 把相似度阈值当成"越高越安全" | 过高压根不命中；过低误命中"相关但答案不同" | `app/cache/settings.py:77-88` 的实测表 |

## 深入关键词

`pgvector` · `vector(n)` · `<=>` 余弦距离 · `HNSW` / `IVFFlat` · `tsvector` / `tsquery` · `to_tsvector` volatility · BM25（tf / idf / avgdl）· `Reciprocal Rank Fusion` · n-gram 索引 · `cosine similarity` vs `L2` 选择
