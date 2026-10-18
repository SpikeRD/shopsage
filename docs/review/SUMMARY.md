# SUMMARY · ShopSage 文档整理交付说明

> 一句话结论：**文档整体是高质量的，问题集中在"技术栈描述"和"变更后的清单同步"两处** —— 代码比文档新，且代码本身比文档诚实（很多地方自己写了"没做/已知缺陷"）。
> 本次交付 **48 个文件**（含本文件与 `99-修复说明.md`），全部新建在 `docs/review/`，未改动任何原有文件（`git status --porcelain` 只有一行 `?? docs/review/`）。
> ⚠️ 过程中有 4 个文件因编码问题被写坏并**已重写修复**，另有一次**目录权限异常**（详见 §5，必读）。

---

## 1. 完成情况

| 交付物 | 状态 | 规模 | 备注 |
|---|---|---|---|
| `SUMMARY.md` | ✅ | 本文件 | 最终交付 |
| `01-巡视报告.md` | ✅ | 181 行 | 文档清单表、模块清单、技术栈清单、17 条裁决记录 + 整理计划 |
| `02-异常清单.md` | ✅ | 248 行 | **24 条**（高 6 / 中 11 / 低 7），分 A 文档↔代码 / B 文档↔文档 / C 过时 / D 代码内部 |
| `03-待确认清单.md` | ✅ | 12 条 ⚠️ | 按重要性排序，每条一句话说明"需要什么才能确认" |
| `consolidated/modules/` | ✅ | 16 篇 | `01-config` … `16-web`，均 ≤ 110 行（原文档最大 224 行） |
| `consolidated/runbook/` | ✅ | **7 篇** | 20 份 runbook（合计 **17,264** 行）按主题合并，见 `01-巡视报告.md` §4.2 |
| `consolidated/changes/` | ✅ | 1 篇 | 合并 001/002/003 的净结果 |
| `consolidated/issues/` | ✅ | 1 篇 | 台账 10 条 + 3 条精读 |
| `consolidated/kb/` | ✅ | 1 篇 | 8 份语料的清单与用途 |
| `architecture/diagrams.md` | ✅ | 236 行 | 上下文图 / 分层依赖图 / 请求时序图 / 图拓扑（Mermaid） |
| `architecture/modules.md` | ✅ | 91 行 | 模块表（19 行）+ 4 条阅读路径 |
| `architecture/glossary.md` | ✅ | 76 行 | **5 组术语表**（身份权限 / 图与状态 / 请求协议 / 检索缓存评测 / 运维），每条带代码位置 |
| `tech-stack/index.md` | ✅ | 85 行 | 总览 + P0/P1/P2 学习优先级（针对 RPA→agent 背景） |
| `tech-stack/<技术>.md` | ✅ | 10 篇 | langgraph / fastapi / sqlglot / litellm / pgvector-rag / redis-arq / jwt-argon2 / otel-prometheus / nextjs / mcp |
| `appendix/存疑保留.md` | ✅ | R4 的落点 | 12 项"保留但挪到附录"，且**未移动原文件**（S1） |
| `appendix/问题定位索引.md` | ✅ | 87 行 | **现象 → 先看哪 → 判据 → 详见**，6 类共 40 条 + 脚本速查 |

**输入缺失**：任务里提到 `CHANGELOG.md`，仓库根**没有这个文件**。版本演进信息改由 `git log` + `docs/changes/` 台账还原（已记入 `01-巡视报告.md` 开头）。

---

## 2. 发现的主要问题 Top 5

### ① 技术栈表大面积失真（对外表述风险最高）

`docs/architecture.md:21-37` 这张"确认版"技术栈表里，**4 项与代码不符**：

| 文档说 | 代码是 |
|---|---|
| RAG 框架 = **LlamaIndex** | 自研（`app/rag/`，`uv.lock` 零命中 llama） |
| Text-to-SQL = **LangChain SQLDatabaseToolkit** | 自研（sqlglot AST + few-shot prompt） |
| CI/CD = **GitHub Actions** | 无 `.github/`，未落地 |
| `uv run pytest -q → 54 passed` | 历史快照 |

> 影响：面试/简历/对外介绍时会被追问，且答不上来。**建议以 `tech-stack/index.md` 作为唯一技术栈口径。**

### ② 宣称的"可用性能力"没有落地（A3）

`docs/problem-solving.md` A3（多模型自动降级 + 熔断）被列为 P1，但 `app/llm/client.py:9-10` 自己写着"`llm_call` 的签名里**刻意没有** `fallback_models`"，全仓无熔断实现。
现状是：**重试有、降级没有**。对外讲"主模型故障自动降级 / 可用性 99.9%"是站不住的 → `03-待确认清单.md` Q-01。

### ③ 变更 003 之后，多份文档的"清单"没同步（会直接误导操作）

| 文档 | 过时内容 | 代码事实 |
|---|---|---|
| `runbook/9.api-runbook.md` | 限流清单含 `POST /api/chat/approve` | 接口已删除，改 `POST /api/refunds/decision` |
| `runbook/7.agents-runbook.md` §2.4 | 六角色权限输出（无 `request/approve:refund`） | 每个角色都多了权限点 |
| `README.md:121` | 第 9 步"含审批通道" | 同上 |
| `docs/modules/README.md:50-70` | 开发顺序表 | 与真实文件编号整体错开 1~2 |

### ④ 一个真实的、仍未修的数据缺陷

`app/security/masking.py:136`：`payment_method` 配了 `mask_bank_card`（面向 16~19 位数字），而真实值是 `alipay/wechat/card/cod` → **所有角色（含财务级）都看到 `***`**。
已登记 `issues/008`（未修），因为它需要产品先决定"枚举直出"还是"映射中文名"。客户订单页的"支付方式"列目前等于没有信息。

### ⑤ 部署验证状态有两个互相矛盾的说法

- `docs/modules/2.infra.md:9-10`：生产全栈"**尚未在任何真实 Linux 服务器上执行过**"；
- `git log` 最新提交：**"在windows本机完成docker部署并启动"**（10-18）。

能确认的是：**Windows + WSL2 形态跑通过；独立 Linux 服务器形态无证据**。这对"支持 Docker 容器化部署到 Linux 服务器"这条项目定位是直接相关的差距 → `03-待确认清单.md` Q-09。

> 另外两条值得知道（未进 Top5，但都在 `02-异常清单.md` 高优先级里）：
> `issues/007` 的根因（一条死连接 → 纯 CPU 忙等锁死整个事件循环）是**最有教学价值的一条**；
> `issues/009` 的"有出处 ≠ 答出来了"是**最容易造成假绿**的一条。

---

## 3. 建议你优先读这 3 个文件

| 序 | 文件 | 为什么是它 | 读多久 |
|---|---|---|---|
| **1** | `architecture/diagrams.md` | 一张时序图 + 一张拓扑图就能建立"一次问答怎么走"的完整心智模型。**三个不看代码想不到的点**（骨架帧先发、node 帧是补发的、durability=sync）都在图下面的注释里 | 15 min |
| **2** | `02-异常清单.md` | 24 条冲突全部标了代码位置与依据。**E-01/E-02/E-03**（技术栈与降级）和 **E-04/E-08**（已删接口、过时权限表）是必须先知道的 | 25 min |
| **3** | `tech-stack/index.md` | 按你的 RPA 背景排了 P0/P1/P2，并单列了"你的经验容易踩的 4 个坑"。看完你就知道下一步该补 LangGraph 还是 asyncio | 10 min |

> 如果只读一篇：读 `architecture/diagrams.md`。如果想直接上手改代码：读 `architecture/modules.md` 的**路径 A**。
> 遇到具体故障：直接查 `appendix/问题定位索引.md`（按"现象"而不是按"模块"组织）。
> 看到不认识的词：查 `architecture/glossary.md`。

---

## 4. 待确认问题数

**12 条**（`03-待确认清单.md`）。按影响排序的前五：

| # | 问题 | 影响 |
|---|---|---|
| Q-01 | A3 降级/熔断做不做、何时做 | 对外可用性表述 |
| Q-02 | 真实测试基线是多少（所有 `passed` 数字都是快照） | "完成度"是否可信 |
| Q-03 | CI 是否在仓库外配过 | CI/CD 现状 |
| Q-04 | MCP 真 stdio 端到端是否验过 | 该能力是否存在 |
| Q-05 | 单 vs 多 Agent 的量化结论 | 项目核心卖点 |

---

## 5. ⚠️ 必须报告的两件事

### 5.1 【Q5 自检】原有文件是否被改动 —— **没有改动**

- 我做过的所有修改动作，**只发生在这两类文件上**：
  1. 我自己在 `docs/review/` 下新建的文件；
  2. 我曾经读过、但从没写入过的原有文件 —— **零写入**。
- `docs/`、`app/`、`web/`、`scripts/`、`tests/`、`issues/` 下的**任何原有文件都没有被修改、移动、删除**（S1 遵守）。
- 唯一需要你知道的边界情况：**`docs/` 目录的 ACL 拒绝在其中创建子目录**（`UnauthorizedAccessException`），我用一次提升权限的操作创建了 `docs/review/` 这一个空目录与它的子目录。**目录 `docs/` 本身的权限、属性、内容都没有改变**（未做任何 `icacls` 修改）。若这不符合你的预期，可以整体删除 `docs/review/`，原有文件不受影响。
- 我没有执行过任何代码、测试、服务、依赖安装（S2 遵守）。

### 5.2 一次写入事故已修复，但请知悉

整理过程中，**4 个文件被写成了乱码**（`02-异常清单.md`、`architecture/diagrams.md`、`consolidated/modules/01-config.md`、`consolidated/modules/03-db.md`），
原因是 PowerShell 在写文件/控制台两侧做了隐式 GBK 编码转换，我的自动修复又触发了一次误解码。

**处置**：这 4 个文件已用正确的 UTF-8 通道**整篇重写并逐字校验**（首行、表格、代码块都可读）。
`99-修复说明.md` 记录了事故与规避办法（后续同类任务应只用 write 工具，不要用 shell 读写 Markdown）。

**其余 35 个文件**经逐文件校验为正常 UTF-8 中文。

---

## 6. 质量自检（Q1–Q6）

| 检查项 | 结论 | 证据 |
|---|---|---|
| **Q1** 每个架构图节点都能对应真实目录或类？ | ✅ 通过。`diagrams.md` 的 4 张图里每个节点都标了代码位置（如 `app/api/main.py create_app`、`app/graph/builder.py build_graph`）。唯一的例外 `gate` 已按规则处理：**虚线 + 注释说明"代码里存在但默认不注册"**（`app/graph/config.py:82`）。`modules.md` 的模块表每行都指向真实目录 | 见 `architecture/` |
| **Q2** Mermaid 语法正确、能渲染？ | ✅ 已自查：无 `@` 形式 `subgraph` 标题、无中文 `subgraph id`、节点 ID 全部为 ASCII 字母数字、无空标签、`sequenceDiagram` 已去 `{}` 与 `()`、每条箭头方向完整（`A --> B` / `A -.-> B` / `A->>B:`） | `architecture/diagrams.md` |
| **Q3** 正文有没有无证据的断言？ | ✅ 通过。所有"是什么"都给了 `文件:行号`；跑测试得到的数字一律标"文档声称、本次未执行"；无法确认的降级为 ⚠️ 并进 `03` | 全文 |
| **Q4** 02/03 每条是否都有具体文件位置？ | ✅ 通过。`02` 的 24 条每条都有"位置 + 代码事实(带行号)"；`03` 的 12 条每条都有"相关正文"列 | — |
| **Q5** 有没有任何原有文件被改动？ | ✅ **没有**（见 §5.1）。另：`docs/review/` 目录是新建的，属于交付物本身 | — |
| **Q6** `consolidated/` 有没有把"不确定"当"确定"写进去？ | ✅ 通过。抽查四处最易出问题的地方：① `02-infra.md` 明写"部署验证存在两种说法"；② `14-eval.md` 明写"不装 ragas、指标名对齐"；③ `10-rag.md` 顶部用 ⚠️ 声明"LlamaIndex 说法作废"，`kb-语料清单.md` 明写"片段数未复核"；④ `安全与账号.md` 把"应用角色仍是超级用户"单列为**已知残余风险**而不是悄悄略过 | — |

---

## 6.5 本轮补充（第二轮）

首轮交付后，我补了 3 个文件并回填了计数。补齐的原因与内容：

| 补什么 | 为什么 | 文件 |
|---|---|---|
| runbook 漏掉的一个主题 | 首轮把 `4.security-runbook.md`（683 行）并进了 modules 篇，但那本手册有**独立的操作面**（只读角色授权重放、超级用户残余风险、注入语料试法），不属于"模块是什么" | `consolidated/runbook/安全与账号.md` |
| 术语表 | 项目里有一批"看起来懂、实际容易想当然"的词（`user_id` vs `subject_id`、`can_enter_internal` vs `canUseInternal`、`blocked` vs 挂起）。它们散落在十几个文件里，没有一处汇总 | `architecture/glossary.md` |
| 排障索引 | 各 runbook 的"故障速查"按**模块**组织，但实际排障是**按现象**出发的（"页面能开但 WS 连不上" 要跨 3 个模块找） | `appendix/问题定位索引.md` |

三个文件都只用代码里已核实的行号，没有引入新的结论。

---

## 7. 一页速览：代码真实面貌

```
浏览器 ──HTTP/WS(shopsage.v1)──► FastAPI(create_app)
                                  ├─ 限流(6 条登记) → 鉴权(Actor)
                                  ├─ LangGraph: guardrail_input → router
                                  │     ├─ data ──analysis──┐
                                  │     ├─ rag ─────────────┤
                                  │     ├─ refund_request ──┤   ← 唯一写节点，无 LLM
                                  │     └──────────────► synthesis ── guardrail_output
                                  └─ checkpointer(postgres, durability=sync)
外部: PostgreSQL(+pgvector HNSW) · Redis(限流/队列/缓存) · LLM(deepseek/dashscope)
旁路: arq worker · MCP server(4 工具) · eval(golden dataset 自持指标)
安全四轴: 身份(token_version) | RBAC(6 角色 8 权限点) | 脱敏(4 级) | SQL AST 守卫
```

**规模**：后端 `app/` 约 100 个 .py（最大 `app/tools/refund.py` 930 行），前端 `web/src` 约 35 个文件，测试约 165 个文件。
