# issue 009 · 语义缓存命中后 `docs` 被清空 → 输出护栏把**缓存里的真答案**判成编造，对客回答变成「抱歉，这个请求我无法处理」

| 项 | 值 |
|---|---|
| 发现时间 | 2026-10-17（复查第 16 步 runbook 的验收时） |
| 状态 | **已修复**（2026-10-17，采用「补事实」修法；见「已做的修复」） |
| 影响面 | **所有**走 `rag` 且**已经进过语义缓存**的问句；对客页与对内页同样受影响。首问正常、再问（含同义改写）被拦 |
| 判据 | 同一句知识库问题连问两次：第 1 次 `flow_state=delivered`，第 2 次起 `flow_state=blocked` + `guardrail_rules=['llm_judge:知识库为空…编造…']`，且 `citations` **仍然非空** |

## 现象

用户报告：对客问答问「退货政策 7天无理由退货」，助手说**没有找到相关知识库片段**；
而同一步 runbook（[`16-web-runbook.md`](../docs/runbook/16-web-runbook.md) §6.2 / §7.4）的验收是正常的。

真浏览器复刻（`--headless=new` + CDP，手法同 runbook §10.4；脚本为临时文件 `.appdata-cdp-repro.mjs`，未入仓）：

```text
========== 第 1 问：退货政策 7天无理由退货 ==========
③ 本轮结束时的气泡与徽章
{ "count": 6,
  "last": "抱歉，这个请求我无法处理。如果你要查询自己的订单、退款或政策，可以直接换个说法问我。",
  "badges": ["对客问答","已连接","已被拦下","路径 rag",
             "guardrail_input","router","rag","synthesis","guardrail_output"] }
③ 出处区（details）
[ "来源 5 条（点开看文档 / 页码 / 片段）退货政策.md#c7e2d414f92714088 …" ]
④ 收到的 answer/done 帧
{ "event":"answer", "data":{ "text":"抱歉，这个请求我无法处理。…",
                             "citations":["docs/kb/退货政策.md #c7e2d41 …", …5 条] } }
{ "event":"done", "data":{"flow_state":"blocked", "route":"rag", "blocked":true} }
```

**关键矛盾**：`citations` 有 **5 条**（检索确实命中、出处确实来自 `退货政策.md`），
而输出护栏的判定理由却是「知识库**为空**，回答编造了条款」。两者不可能同时为真 —— 这就是根因所在。

后端日志（同一时刻）逐条对应：

```text
guardrail 拦下请求：stage=output rules=['llm_judge:回答引用[1]-[5]等条款，但材料知识库0条、无出处，属编造材料外内容'] detail=LLM 判定不合规
guardrail 拦下请求：stage=output rules=['llm_judge:知识库与业务数据均为0，回答却引用[1]-[5]编造退货条款、时限与运费规则'] detail=LLM 判定不合规
```

## 根因

三处各自的实现都「对」，接起来才错：

```text
① app/agents/rag.py::rag_node 命中语义缓存时
     return {"docs": [], "citations": [...], "cached_answer": ..., "cache_info": {...}}
                    ↑ 刻意留空：缓存命中的是「整段答案」，不是「检索结果」
                       结果：这一轮**没有检索**，所以没有 docs

② app/agents/guardrail.py::_materials_digest（输出侧护栏的「材料摘要」）
     docs = list(state.get("docs") or [])
     lines.append(f"- 知识库片段：{len(docs)} 条 …")      ← 恒为 0 条
     # 只喂了 rows / docs / analysis / refusals / refund，
     # **没有**把 cached_answer 与 citations 喂进去

③ app/agents/synthesis.py 命中缓存时
     if cached: return {"answer": cached, "citations": citations_of(state.get("docs")) …}
                ↑ 缓存里那句答案是**上一轮真检索出来的真话**，带 [1]~[5] 编号引用
```

于是判定器看到的是「材料：知识库 0 条」+「待审回答：满是 `[1]`~`[5]` 的退货条款」，
它的推理**完全正确**：按它拿到的材料，这些条款确实没有出处 —— 它拦的是「编造」。
**是我们漏喂了「这一轮用了缓存答案」这条事实**，与
[`app/agents/guardrail.py::_materials_digest`](../app/agents/guardrail.py) 里已经记录过的
**变更 003 假阳性（`state["refund"]`）是同一类缺陷的复发**：

> 新增一条「不经过检索、直接由节点产出事实」的路径时，同时要更新所有「核对有没有编造」的下游，
> 否则护栏会把**真话**当成编造。

第 13 步加语义缓存时新增的正是这样一条路径，而 `_materials_digest` 没有跟着更新。

### 直接证据（进程内复现，把每一步的 state 打出来）

用 `GraphRuntime(cache=build_cache())` 跑同一句、两个**全新的 thread_id**：

```text
--- run1 (new thread) ---
  blocked: False   rules: []
  cache_info: {'outcome': 'miss', 'detail': '最近邻相似度 0.3936 低于兜底线 0.7', …}
  docs: 5  citations: 1  cached_answer present: False
  materials:
      - 业务数据：0 行
      - 知识库片段：5 条（出处：docs/kb/会员积分.md, …）      ← 有材料
  answer: 可以的，积分能抵现。 - 抵扣比例：100 积分抵 1 元 …[1]

--- run2 (new thread) ---
  blocked: True    rules: ['llm_judge:知识库为空，回答编造了100:1比例、30%上限、24个月有效期等材料中没有的数字']
  cache_info: {'outcome': 'hit', 'detail': '完全同问', 'age_seconds': 3.796, 'similarity': 1.0, 'cached_answer_used': True}
  docs: 0  citations: 1  cached_answer present: True        ← docs 归零
  materials:
      - 业务数据：0 行
      - 知识库片段：0 条（出处：无）                          ← 护栏看到的就是这一行
  answer: 抱歉，这个请求我无法处理。…
```

run1 与 run2 的**答案正文完全相同**（同一个缓存字符串），差别只在 `docs` —— 一个放行、一个拦下。

### 为什么「换个说法」也躲不掉（语义缓存是阈值命中）

缓存是**语义**命中，不是精确匹配，所以同义改写会命中同一条（`semantic_threshold=0.85`）：

```text
== scope: admin 0
   '退货政策是怎么规定的？'     hit=True  完全同问 sim=1.0
   '退货政策内容是什么？'       hit=True  语义命中 sim=0.9012     ← 改写也命中
   '你们的退货规则有哪些条款？' hit=True  完全同问 sim=1.0
```

对客实测（连问三次同一意图）：

```text
'退货政策 7天无理由退货'            -> blocked cites=5
'7天无理由退货政策是什么'            -> blocked cites=5   ← 改写，仍命中同一条缓存
'请问退货政策里7天无理由是怎么说的'  -> blocked cites=5   ← 再改写，仍命中
```

也就是说：**一旦某句话被问过一次并且答对了，这个意图从此就答不对了** ——
用户越确认越糟，且**刷新页面、换会话都没用**（缓存在 Postgres 里，跨进程跨会话）。

## 为什么第 16 步的验收「是正常的」（这条最值钱）

`web/tests/live/live.test.ts` 的「真溯源」用例与 runbook §6.2 的闭环判据**只断言 citations**：

```ts
const state = await askOnce(TOKEN_CUSTOMER, "7天无理由退货的运费由谁承担？");
assert.ok(state.citations.length > 0, "知识库问题应当带出处…");
const parsed = parseCitation(state.citations[0]);
// …只验出处能解析、/api/kb 能读回原文
```

而缓存命中时 `citations` **照常非空**（`rag_node` 从缓存里带出来的是上一轮的真出处），
所以这条用例在**完全坏掉的路径上依然全绿**。实测（同一时刻、同一后端）：

```text
$ node --test --test-isolation=none tests/live/live.test.ts
✔ 真流式：一帧不漏地走完 skeleton→node→answer→done（B2 的首帧 < 1s）
✔ 真溯源：问知识库问题 → answer 帧带出处，且能被 parseCitation 解析
ℹ tests 10   pass 10   fail 0          ← 全绿

$ 同一句走 API 三次
run1: flow=blocked cites=3 answer='抱歉，这个请求我无法处理。…'
run2: flow=blocked cites=3 answer='抱歉，这个请求我无法处理。…'
run3: flow=blocked cites=3 answer='抱歉，这个请求我无法处理。…'
```

**「有出处」不等于「用户拿到了回答」**。第 16 步 §7.7 那张「什么证据能抓到什么」的表里
缺了第三列：`citations` 非空是**必要条件，不是充分条件**。

> ⚠️ 而且这条用例**自己就是污染源**：它每次运行都会把
> 「7天无理由退货的运费由谁承担？」写进缓存，于是**第二次跑就命中**，
> 从此这条「真链路证据」实际测的是缓存路径。

## 判据（怎么区分「正常」与「这一条」）

| 观察 | 正常 | 本条缺陷 |
|---|---|---|
| `citations` | 非空 | **也非空**（所以不能只看它） |
| `flow_state` | `delivered` | **`blocked`**（第 2 次起） |
| `answer` 正文 | 具体条款 | `抱歉，这个请求我无法处理。…`（`BLOCKED_CUSTOMER_MESSAGE`） |
| 后端日志 | 无 `guardrail 拦下请求` | `rules=['llm_judge:…知识库…0 条…编造…']` |
| `cache_info.outcome` | `miss` | **`hit`** + `cached_answer_used: true` |
| 换新 `thread_id` / 刷新页面 | 恢复 | **不恢复**（缓存在库里） |
| 换一个**完全没问过**的问法 | 正常 | 正常（所以要「换一句话」才能问出答案） |

最后一行是给用户的临时绕法，也是最好的自检手法：
**同一意图用一句「从没出现过的措辞」再问一次 —— 若这次正常，就是本条。**
（`semantic_threshold=0.85` / `semantic_floor=0.7`，改写得太近仍会命中。）

## 影响

* **对客**：命中缓存的常见问题（退货政策、积分、运费…）一律变成「无法处理」，
  而 citations 还挂在下面 —— 用户看到的是「有来源但不回答」，比「查不到」更困惑；
* **对内**：同样受影响（`_materials_digest` 不区分角色）；
* **运维**：日志里全是「编造」告警，会把真正的编造告警淹掉（告警疲劳）；
* **不挡开工**，但**会误导判断**：它长得像「模型在编造」，而实际是护栏缺材料 ——
  按「模型编造」去查会一路查到 prompt 层（那两个地方都是对的）。

## 已做的修复（2026-10-17，采用第 1 条「补事实」修法）

只动了一处：[`app/agents/guardrail.py::_materials_digest`](../app/agents/guardrail.py)。
缓存命中时，材料摘要里多出一行**显式事实**：

```text
- **本轮复用语义缓存里的既有答案，相似度 1.0000**：该答案由上一轮**真实检索**生成，
  出处共 5 条（docs/kb/退货政策.md #c00bcbd…, …）。回答里的 `[n]` 编号是**如实的**，
  不是编造 —— 不要因为「本次没有重新检索」就判它编造。
```

两个细节：

* **两个判据都认**：`cached_answer_used`（`synthesis` 写）与 `outcome == "hit"`（`rag_node` 写）。
  只认一个就会在「上游把另一个键覆盖掉」时静默失效 —— 而那种失效没有任何运行期表现。
* **只在命中时出现**：未命中/正常检索的轮次摘要里**不许**出现「缓存」二字，
  否则判定者会以为答案来路不明（有一条用例专门钉这个）。

与 `refund` 那段是**完全同构**的修法：不降审查强度，只把漏掉的**事实**补上。

### 实测证据

```text
# 同一句连问 3 次（第 2 次起必然命中缓存）
[customer run1] delivered cites=5 '7 天无理由退货的规则是这样：…'
[customer run2] delivered cites=5 '7 天无理由退货的规则是这样：…'   ← 修复前这里起全是 blocked
[customer run3] delivered cites=5 '7 天无理由退货的规则是这样：…'

[admin    run1..3] 退货政策是怎么规定的？      -> delivered / delivered / delivered
[admin    run1..3] 七天无理由退货有吗          -> delivered / delivered / delivered
[customer run1..3] 会员积分怎么获得？          -> delivered / delivered / delivered
```

真浏览器（`--headless=new` + CDP，同 runbook §10.4）连问两次同一句：

```text
第1问: 退货政策 7天无理由退货 → ✅ 已答完
第2问: 退货政策 7天无理由退货 → ✅ 已答完   ← 修复前第 2 问是「❌ 已被拦下」
```

### 防回归（已落地）

`tests/cache/test_cache_revision_invalidation.py` 里 3 条：

* 命中缓存 → 摘要必须含「缓存」且含「出处共 N 条」；
* 未命中 → 摘要**不许**含「缓存」；
* 两种判据形状（`cached_answer_used` / `outcome=hit`）都要被认。

## 验收口径也改了（2026-10-17，与用例同步）

只改用例、不改文档，会出现「代码里的验收通过了、runbook 上的判据还是旧的」——
那正是这条 issue 的成因本身（判据写漏了一步）。所以两处一起改：

| 位置 | 原来 | 现在 |
|---|---|---|
| `web/tests/live/live.test.ts`「真溯源」 | 只断言 `citations` 能解析、能点开 | **加断言** `flowState === "delivered"`、`blocked === false`、正文不含「无法处理」；且**连问两次** |
| `16-web-runbook.md` §6.2 验收第 2 条 | 「闭环」四步（第一步就是 `citations` 非空） | 拆成 **① 答出来了**（第一判据）+ ② 溯源闭环，并注明「必须连问两次」 |
| `16-web-runbook.md` §7.7 证据表 | 六行 | 补两行（issue 009 / 010），并加一条结论：**「断言的东西在」≠「用户拿到了东西」** |
| `16-web-runbook.md` §6.2.1（新增） | —— | 缓存这两条修法各自的单独验法（空结果 / 灌库清缓存） |

**为什么「连问两次」是必须的**：第 2 次必然走语义缓存，而这条 bug
**只在第 2 次起发作**。只问一次的用例，无论断言写得多严，都永远测不到那条路。

**「去掉就红」已验证**（这一步不做就等于没写用例）：

```text
# 把 guardrail.py 的缓存补事实那段 stash 掉 → 重启后端 → 跑真链路
✖ 真溯源：问知识库问题 → **答出来了** 且 answer 帧带出处、能解析、能点开
  AssertionError: 第 1 次提问没有答完（flow_state=blocked，blocked=true）：
    回答=抱歉，这个请求我无法处理。… —— 若这是第 2 次，先怀疑语义缓存那条路（issue 009）
ℹ pass 7  ℹ fail 1        ← 修复前这里是 pass 10 / fail 0（全绿）
```

恢复修复、重启后端后回到 `pass 10 / fail 0`。

⚠️ 复现这个验证时踩了一个**已知坑**：`git stash` 之后**必须重启后端** ——
本仓 `uvicorn` 不带 `--reload`（runbook 坑 8），不重启的话跑的还是旧代码，
第一次验证会得到假的「全绿」。

## 相关

* 同源缺陷（同类修法的先例）：[`app/agents/guardrail.py::_materials_digest`](../app/agents/guardrail.py) 里
  「变更 003：**必须**把退款申请的结果写进来（实测踩到的假阳性）」
* 缓存闸的位置与取舍：[`app/agents/rag.py::rag_node`](../app/agents/rag.py)（第 13 步）
* 被这条缺陷掩盖的另一条问题：[`issues/010-semantic-cache-serves-stale-empty-answers.md`](010-semantic-cache-serves-stale-empty-answers.md)
* 第 16 步验收的判据缺口：[`docs/runbook/16-web-runbook.md`](../docs/runbook/16-web-runbook.md) §6.2 / §7.4 / §7.7
