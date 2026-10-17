# issue 010 · 语义缓存复活「检索回 0 条」的旧答案：知识库已修好，缓存还在讲「没有找到相关知识库片段」

| 项 | 值 |
|---|---|
| 发现时间 | 2026-10-17（排查 issue 009 时顺手撞到） |
| 状态 | **已修复**（两条修法都落地：灌库推进版本 + 空结果不缓存） |
| 影响面 | 知识库/检索修好**之后**，曾经问过且当时检索为空的问法；TTL 内（政策类 **7 天**）都答不对 |
| 判据 | `cache_semantic_entries` 里存在 `citations=[]` 且答案含「没有找到/无命中/看不到」的条目，`corpus_revision` 与当前一致（=没被失效） |

## 现象

后台库里现在躺着两条**知识库明明灌好了（61 活跃片段 / 8 份文档）**却仍回答
「检索无命中」的缓存条目：

```text
rev=1 [admin:0] '退货政策是怎么规定的？'   created=2026-10-17 07:47:47  exp=2026-10-24  cites=0
rev=1 [admin:0] '七天无理由退货有吗'       created=2026-10-17 07:48:22  exp=2026-10-18  cites=0
```

答案原文（**这就是用户说的「没有找到相关知识库片段」那类话术**）：

```text
结论：无法确认是否支持「七天无理由退货」。本次可用的知识库片段为「无」（未检索/未命中/后端不可用），
业务数据也为「无」（未查询/查询结果为 0 行），因此没有任何可核对的政策条款或订单记录，
不能据此给出「有」或「没有」的结论，也不做推测。

无法回答退货政策的具体规定：本次知识库检索无命中（或后端不可用），业务数据查询结果为空（0 行），
材料中没有关于退货政策的任何条款、时限、运费承担或退款口径信息。…
```

而**同一时刻、同一个后端**，用同一角色的**新问法**问就会命中知识库：

```text
'退货政策是怎么规定的？'      -> flow=delivered cites=0   ← 缓存的旧答案（0 出处）
'退货政策内容是什么？'        -> flow=delivered cites=0   ← 语义命中同一条（sim=0.9012）
'你们的退货规则有哪些条款？'  -> flow=delivered cites=5   ← 未命中缓存，正常！
'换货政策是怎么规定的？'      -> flow=delivered cites=5   ← 正常
```

`cites=0` 与 `cites=5` 的分界**完全是缓存命中与否**，与知识库、检索、模型都无关。

## 根因

两层原因叠在一起：

### ① 空结果被当成正常答案写进了缓存

[`app/agents/synthesis.py`](../app/agents/synthesis.py) 写缓存的判据只有「回答非空」：

```python
out: dict[str, Any] = {"answer": answer, "citations": citations}
if cache is not None:
    write = await remember_answer(state, answer, cache=cache, citations=citations)
```

它不区分「**检索到了、有出处**」与「**检索回 0 条、如实说没有**」。
后者是一次**瞬时事实**（语料没灌 / 检索后端抖动 / 问法没命中），把它当答案缓存 7
天（`policy_ttl_seconds=604800`），等于把一个故障固化下来。

⚠️ 这正是 [`app/agents/rag.py`](../app/agents/rag.py) 里那条注释想避免的、但方向反了：

```python
# 命中为 0 不写 refusals：那是**数据事实**（知识库里确实没有），
# 由 synthesis 如实表述；把它写成 refusal 会与「能力未接上」混成一类。
```

「命中 0 条」确实**可能**是数据事实，但它同样可能是
`load_knowledge_base` 还没跑完（runbook §10.1 坑 6：全量回归会把 `rag_chunks` 清空）。
**两种情况在缓存里长得一模一样，而缓存把两者一起固化了。**

### ② 知识库重新灌入**不会**让这些条目失效

第三级失效（版本失效）的接线是：
`app/rag/ingest.py::ingest_document(on_change=...)` → `bump_after_ingest` → `corpus_revision + 1`
→ 所有 `fingerprint` 变化 → 整批语义失效。

而实际灌库用的是 [`scripts/load_knowledge_base.py`](../scripts/load_knowledge_base.py)，
它调 `ingest_path` / `ingest_document` **时没有传 `on_change`**：

```python
report = await ingest_path(
    root, category=args.category, store=store, settings=settings, force=args.force,
)   # ← 没有 on_change=…
```

所以 `corpus_revision` 至今仍是 **1**，两条旧条目 `corpus_revision=1` 与当前一致 → 判定为「新鲜」。

## 为什么这条比 issue 009 更隐蔽

issue 009 的表现是「**有出处但不回答**」（`citations` 非空 + `blocked`），一眼能看出矛盾。
本条的表现是「**回答得很认真，但内容过时**」：

* `flow_state=delivered`、`blocked=False`、没有任何告警；
* 只有 `citations=[]` 是线索，而**「知识库确实没有这条」也会给出 `citations=[]`** ——
  两者在前端完全不可区分（runbook §9.1 那条「回答出来了但没有来源」正好指向这里）；
* 更糟的是它的**文案极其像样**（「不能据此给出『有』或『没有』的结论，也不做推测」），
  用户会相信这套说辞，而不是怀疑缓存。

## 判据（怎么区分「知识库真没有」与「这条」）

| 观察 | 知识库真没有 | 本条（缓存固化的空结果） |
|---|---|---|
| 同一问法的**新措辞** | 仍然 0 出处 | **能查到**（`citations` 非空） |
| `cache_semantic_entries` 里该问法 | 无条目（或本次新写） | **已有条目**，且 `citations=[]`、`created_at` 早于灌库时间 |
| `corpus_revision` | 不相关 | 条目 revision == 当前 revision（说明**没被失效**） |
| 灌库之后立刻问 | 0 出处 | **仍然 0 出处**（这就是本节要抓的） |

一条命令看清（在仓库根，改了 `.env` 的库地址就会跟着走）：

```powershell
# 抽样打印条目（含答案摘要）—— 看 citations=[] 且答案说「无命中」的那些
uv run python -m scripts.cache_report --sample 20
# 只看某个身份
uv run python -m scripts.cache_report --scope admin:0 --sample 20
# 实测输出（本次排查时）：
#   [admin:0] '退货政策是怎么规定的？'  created=2026-10-17 07:47:47  exp=2026-10-24  cites=0
#   [admin:0] '七天无理由退货有吗'      created=2026-10-17 07:48:22  exp=2026-10-18  cites=0
```

最小的判据是那句**「换个没问过的说法再问一次」**：
如果换措辞就答上了，说明知识库有料 —— 那么原问法答不上就是本条（或 issue 009）。

## 影响

* **会误导判断**：现象与「语料没灌」（runbook §10.1 坑 6）**一模一样**，
  而坑 6 的处置是「重新灌库 + 重启后端」—— 灌完仍然复现，然后就会去查检索器、embedding、
  问法……实际只需要让那条缓存失效；
* **持续 7 天**（政策类 TTL），且**跨会话、跨进程、跨重启**（缓存在 Postgres 里）；
* 与 issue 009 叠加后更难分：
  issue 009 让「有出处的」答不出来，本条让「没出处的」看起来很正常。

## 已做的修复（2026-10-17）

采用**第 2 条修法**：让灌库真的推进 `corpus_revision`，于是整批旧条目按 `fingerprint` 失效。
四处改动 + 一组用例：

| # | 位置 | 改了什么 | 为什么必须改 |
|---|---|---|---|
| ① | `app/rag/ingest.py::IngestReport` | 新增 `doc_changed` 属性（`action != "unchanged"`，**含 `deleted`**） | 这个属性**从来不存在**，而 `ingest_document` 早就拿它当 `on_change` 的判据 —— 见下「最关键的发现」 |
| ② | `app/rag/ingest.py::ingest_document` | 把「读 `doc_changed`」也搬进 `try` | 原先判据在 `try` **之外**：判据一出错就炸掉整次入库（入库其实已经成功了） |
| ③ | `app/rag/ingest.py::ingest_path` / `delete_document` | 补 `on_change` 参数并透传 | 灌库脚本走的是 `ingest_path`（整目录）；它不接收就等于**静默丢掉**。下线同样是知识库变更 |
| ④ | `app/rag/sync.py::RagSyncWorker` | 构造函数补 `on_change`，`sync_once` 透传 | 定时同步是生产上**唯一会自动改知识库**的路径，不接线 = 每天留一份旧缓存 |
| ⑤ | `scripts/load_knowledge_base.py` | 两条路径（`--limit` 与 `ingest_path`）都传 `on_change=make_ingest_callback()`，`--prune` 也传 | 缺的就是这一行；另加 `--no-invalidate` 给排障留出口 |

### 最关键的发现：这条接线**根本接不上**

`ingest_document` 在很久以前就写了：

```python
if on_change is not None and report.doc_changed:   # ← 这句在 try 之外
    try: on_change(report)
```

而 `IngestReport` 上**从来没有** `doc_changed` 字段：

```text
>>> IngestReport(source="x", action="updated").doc_changed
AttributeError: 'IngestReport' object has no attribute 'doc_changed'
```

所以任何调用方只要真的传了 `on_change=`，**第一次入库就崩**。
这不是「缓存偶尔不失效」，而是「这条接线没人敢接」—— 也解释了为什么
`load_knowledge_base.py` 一直没传它：传了就跑不通。

### 实测证据

```powershell
# ① 改一份知识库文档 → 灌库：版本推进，且日志点名是哪一份
$ uv run python -m scripts.load_knowledge_base --source "docs/kb/退货政策.md"
INFO app.rag.ingest: 入库完成：docs/kb/退货政策.md action=updated 片段=12（新增 1 / 更新 0 / 未变 11 / 失效 1）
INFO app.cache.corpus: 知识库版本 → 10（by=ingest:docs/kb/退货政策.md, document_version=2）

# ② 全量灌库：8 份文档各推进一次（1 → 9）
$ uv run python -m scripts.load_knowledge_base
INFO app.cache.corpus: 知识库版本 → 2 … → 9（每份一份 bump）

# ③ **反向**：内容没变再灌一次 —— 版本**不动**（不白清缓存）
$ uv run python -m scripts.load_knowledge_base
  documents 8 / created 0 / updated 0 / unchanged 8
  revision before: 9 → after: 9        ← 9 条用例里有一条钉的就是这个

# ④ 旧条目真的不再命中（revision 9 的条目 vs 当前 10）
$ select scope, question, corpus_revision, is_active from cache_semantic_entries order by created_at desc
  rev=10 active=True  '换货有什么限制？'   ← 灌库后**重新检索**写入的新条目
  rev=9  active=True  '换货有什么限制？'   ← 旧条目：行还在，但 fingerprint 不同 → 永不命中
```

**判据**：`fingerprint` 里带 `corpus_revision`，而 `nearest()` 的 SQL 按
`fingerprint` 过滤（`app/cache/stores.py::PostgresEntryStore.nearest`），
所以推进版本 = 整批旧条目**立刻不可见**（不必逐行删，也不必等 TTL）。

### 防回归

`tests/cache/test_cache_revision_invalidation.py`（9 条，全绿）：
`doc_changed` 三态（updated/unchanged/**deleted**）/ `ingest_path` 与 `delete_document`
必须收 `on_change` / `RagSyncWorker` 必须收 `on_change` / 三种变更 action 都要触发。

⚠️ 这些断言**故意测签名与属性存在性**（而不是「跑一遍真库」）：
这类缺陷没有任何运行期表现 —— 漏接线时程序一切正常，只是缓存悄悄不失效。

## 第 1 条修法：空结果不进缓存（2026-10-17 追加，产品已确认）

产品决定：**空结果不要缓存**。落地在 `app/agents/synthesis.py`，判据是：

```python
writes_cache = bool(state.get("cache_context")) and bool(citations)
```

两个条件缺一不可，含义是「**这一轮真的走了知识库分支**（`cache_context` 存在）
**且至少有一条出处**」。不满足就跳过 `remember_answer`，并在 `cache_info`
里留痕 `write: "skipped:empty_citations"`（**默默跳过比报错更难查**，
所以要能在一轮的状态里看见它为什么没写）。

### 为什么判据必须带 `cache_context`（这里我一开始想错了）

我最初的担心是：退款路径的 `citations` 恒为空，只按 `citations` 判空会把它一起误伤。
**实测证明这个担心是多余的** —— `remember_answer` 的第一句就是 `if not context: return None`，
而 `cache_context` **只有 `rag_node` 会写**，退款走 `refund_node`，本来就没有它。

但这个纠正有价值，值得写下来：**把判据写成显式的 `cache_context and citations`，
而不是依赖下游的短路**。理由是 `remember_answer` 的 `context` 要求将来一旦放宽
（比如有人想给退款路径接缓存），显式判据仍然正确，而靠短路的写法会静默改变语义。

### 实测证据

```text
# 注入一个「永远返回 0 条」的检索后端（模拟语料被清空 / 检索未命中）
$ 第1次 write: skipped:empty_citations
$ 第2次 outcome: miss | write: skipped:empty_citations
   ↑ 第二次仍然 miss = 重新检索了，没有被缓存固化
```

**反向**（更要紧）：有出处的答案必须照常写缓存 —— 否则这次修法把 E1 的收益清零。
`tests/cache/test_cache_empty_result_not_cached.py` 用 `RecordingCache`
（记录 `remember_answer` 有没有被调用）钉住了这一侧：

| 场景 | 期望 |
|---|---|
| 走了知识库 + 0 条出处 | **不写**，`write=skipped:empty_citations` |
| 走了知识库 + 有出处 | **照写**（断言 `records == 1`） |
| 退款分支（无 `cache_context`） | **本来就不写**（这是既有语义，不是本次改的） |

4 条用例全绿。⚠️ 特别注意第二条：**修「空结果不缓存」时最容易顺手把「有结果也不缓存」** ——
那样所有用例照样绿，而缓存的收益归零。

### 这一步的边界（说清楚，免得误读）

它解决的是「**检索当时回 0 条**被固化」。它**不**解决：
`citations` 非空但内容**过时**（那由第 2 条修法、即灌库清缓存负责）。
两条合起来才覆盖完整：

| 坏情况 | 谁负责 | 判据 |
|---|---|---|
| 检索回 0 条被缓存 7 天 | **本条**（不写） | `cache_context` 有、`citations` 空 |
| 答案对但知识库已更新 | 第 2 条（灌库 bump） | `corpus_revision` |

## 相关

* 掩盖它的是 issue 009：[`issues/009-semantic-cache-hit-empties-docs-guardrail-blocks.md`](009-semantic-cache-hit-empties-docs-guardrail-blocks.md)
  （有出处的缓存条目被护栏拦掉，于是「缓存命中」这个共同根因在两种表象之间来回躲）
* 语义缓存的设计与失效策略：[`docs/runbook/13.cache-runbook.md`](../docs/runbook/13.cache-runbook.md) §5
* 现象极像的既有坑：[`docs/runbook/16-web-runbook.md`](../docs/runbook/16-web-runbook.md) §10.1 坑 6（语料被全量回归清空）
* 「回答出来了但没有来源」那一行：同上 §9.1（现在需要补一条「先怀疑缓存」）
