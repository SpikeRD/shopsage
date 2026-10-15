# 001 · 对内报表「出图」是非确定性的，且探针脚本的报错不指向真因

* **状态**：部分处理（脚本侧已修；模型侧未修）
* **发现于**：2026-10-15，新机器环境迁移验收（[`docs/环境迁移验收.md`](../docs/环境迁移验收.md)）
* **归属模块**：第 7 步 `app/agents/analysis.py`（prompt / JSON 解析健壮性）；
  第 16 步 `scripts/probe_internal_query.py`（脚本可用性，**已修**）
* **相关文档**：[`docs/runbook/16-web-runbook.md`](../docs/runbook/16-web-runbook.md) §6.4

---

## 现象

照 runbook §6.4 的验收第 4 条跑：

```powershell
uv run python -m scripts.probe_internal_query
```

### 现象 A：脚本直接崩（后端没起时）

```text
Traceback (most recent call last):
  ...
  File "scripts/probe_internal_query.py", line 49, in main
    print(f"后端 {API_BASE} → HTTP {health.status_code} status={health.json().get('status')}")
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

### 现象 B：后端起好了，但 5 句问法不是每句都出图

实测 5 句 × 2 轮（后端在跑）：

| 问法 | 出图率 | 失败时的 `refusals` |
|---|---|---|
| 各个订单状态的订单数量，用柱状图 | **2/2** | — |
| 按订单状态统计订单数量 | **1/2** | `没有可分析的数据行：analysis 需要先由 data 节点查出数据…` |
| 统计每种订单状态各有多少笔订单，画成柱状图 | **2/2** | — |
| 最近 30 天每天的订单金额趋势，用折线图 | **0/2** | `analysis 节点的 LLM 连续 2 次未给出可解析的 JSON 对象，已放弃（最后一次原因：not_json）` |
| 销售额最高的 10 个商品，用柱状图 | **1/2** | 同上，另有一次连带 `synthesis 节点…not_json` |

---

## 影响

* **现象 A** 的报错**完全不指向真正的原因**：看到 `JSONDecodeError` 会以为脚本/代码坏了，
  实际只是「后端没在跑」。而脚本文件头第 10 行本来就写着前提是「对着真正在跑的后端」——
  前提写在注释里，**运行时却不校验**。
* **现象 B** 会让验收**假红**：runbook §6.4 当时的记录是「五种全部出图」，
  照它验收就会把**模型的随机性**当成「功能坏了」，进而去查环境/前端 —— 方向全错。
* 两者叠加的后果：一次无关代码的失败，要花时间去排除「是不是迁移把环境搞坏了」。

---

## 根因

### A：脚本没校验探活结果

`health.json()` 在后端未启动时拿到空响应（`httpx` 不抛异常，状态码可能仍是 200/502），
`.json()` 于是抛 `JSONDecodeError`。**是脚本的健壮性缺陷，不是环境问题。**

### B：LLM 被要求输出严格 JSON，偶有给不出来

`app/agents/analysis.py` 的 system prompt 里写着硬约束：

```python
"4. 只输出 JSON，不要解释。",
output_schema='{"headline": "…", "bullets": ["…"], "caveats": ["…"]}',
```

模型偶尔返回的不是合法 JSON → 节点重试 2 次仍失败 → 抛 `AgentOutputError` → **这一次没图**。
另一类失败（`没有可分析的数据行`）发生在更上游：那一次 `data` 节点的 Text-to-SQL 没查出数据。

📌 **两类失败都不在「问法」上，也不在前端**，而是「模型输出」这条链的固有波动 ——
这与 §6.5 记录的「同一件事两种问法出一条/不出出处」是**同一性质**的问题，
只是发生在 `analysis` / `synthesis` 而不是 `rag`。

---

## 判据（**这条最重要，防止后人误报**）

| 观察 | 判定 |
|---|---|
| 5 句里**至少一句稳定出图**，失败项原因是 `not_json` 或 `没有可分析的数据行` | **正常**，属已知非确定性 |
| 5 句**一句都不出图** | **异常** —— 按 §6.4 末尾的顺序查：先看后端日志有没有 `sql_query` 的 `tool_error`，再看前端 |
| 脚本抛 `JSONDecodeError` | **后端没起**（或地址写错），与代码无关 |
| 同一句话重跑结果不同 | **正常**，正是本文记录的非确定性 |

⚠️ **不要把「少于 5 句出图」当作回归失败**。

---

## 处理

### 已做（2026-10-15）

1. **`scripts/probe_internal_query.py`**：探活改为自己判「后端在不在」。
   连不上时打印一句可照做的提示（含起后端的命令与 `SHOPSAGE_API_BASE` 覆盖方式）
   并以**退出码 1** 结束，不再抛 `JSONDecodeError`。
2. **同脚本**：结尾新增「出图 N/5 句」汇总 + 非确定性说明，避免读者把单次结果当回归基线。
3. **`docs/runbook/16-web-runbook.md` §6.4**：验收口径从「5/5 全出图」改成
   **「看失败原因能不能解释」**，并补上 5×2 轮的实测出图率与两类失败原因。

### 未做（留给后续）

* **`analysis` / `synthesis` 节点的 JSON 解析健壮性**（真正的根治）：
  可选方向 —— 提示词里给出更明确的 JSON 示例、失败时把模型的原始输出回灌重试、
  或改用厂商的结构化输出/JSON mode（`deepseek` 是否支持需实测）。
  **这属于第 7 步的改动，会动到已验收的 prompt，需单独评估，不在本次迁移范围内。**
* 是否值得把这条链路做成「重试到出图为止」（真链路用例 `live.test.ts` 的
  「对内报表」那一条已经是这么做的，最多 3 次）——待定。

---

## 复现命令（供后人核对）

```powershell
# ① 先起后端（前置条件，脚本不会替你起）
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# ② 另开一个终端
uv run python -m scripts.probe_internal_query
```
