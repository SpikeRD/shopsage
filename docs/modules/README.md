# 模块开发与测试文档索引

> 本目录是「每个模块的开发 + 测试文档」，与 [`docs/architecture.md`](../architecture.md)（蓝图）和
> [`docs/problem-solving.md`](../problem-solving.md)（验收清单）配套使用。
> 每个模块文档统一采用 **8 段模板**，开发一个模块前先读它对应的文档，开发完按「验收标准」自检并回填证据。

> 🛠 **实操手册**（在 [`docs/runbook/`](../runbook/) 下，编号与本目录一致 —— 第 N 步的模块文档配 `N.*` 手册）：
> [`1.config-guide.md`](../runbook/1.config-guide.md)（配置取值 / 改法 / 验证）、
> [`2.infra-runbook.md`](../runbook/2.infra-runbook.md)（本机 VMware-Windows 没有 Docker，依赖栈跑在 VMware-Linux 上：
> 启动顺序、期望输出、故障速查、证据回填格式照抄即可）、
> [`3.db-runbook.md`](../runbook/3.db-runbook.md)（建表 / 迁移 / 种子数据 / 连接池与行级过滤的逐步验证）、
> [`4.security-runbook.md`](../runbook/4.security-runbook.md)（SQL 白名单 / 越权拦截 / 脱敏 / 鉴权 / 只读角色）、
> [`5.llm-runbook.md`](../runbook/5.llm-runbook.md)（LiteLLM 门面 / 429 指数退避重试 / `embed()`；含依赖安装与离线 mock 测法），
> 另有不绑定某一步的底座命令手册 [`4.uv-pytest-basics.md`](../runbook/4.uv-pytest-basics.md)（uv 环境与 pytest 选用例）。
>
> 📌 **本目录文件已按「建议开发顺序」编号**（`1.config.md` … `16.web.md`，与下表 `#` 一致；
> README 自身不编号）。下表第 11 步复用第 7 步的文件，所以这里只出现一次 `7.agents_module.md`。

## 8 段模板说明

| 段 | 回答的问题 |
|---|---|
| 1. 职责 | 这个模块到底干什么、边界到哪、不负责什么 |
| 2. 对外接口 | 别人怎么调它（函数签名 / 路由 / 消息 / 协议） |
| 3. 依赖 | 依赖哪些内部模块 + 哪些外部库/服务 |
| 4. 关键设计决策 | 为什么这么设计、舍弃了什么、取舍理由 |
| 5. 核心技术点 | 必须真正弄懂的「为什么」（面试/复述重点） |
| 6. 开发步骤 | 可执行的顺序，含关键命令 |
| 7. 测试用例 | 具体可写的测试（含攻击/异常用例） |
| 8. 验收标准 | 关联 problem-solving 条目 + 量化指标 + 证据链接空位 |

## 模块清单与建议开发顺序（按真实依赖关系排序）

> 排序三原则：**① 依赖驱动**（前一步是后一步的地基）→ **② 每步结束都能跑能测**（不留半成品）→
> **③ 先跑通 happy path，再加固/降本**（重试、熔断、缓存、评测都不在第一步就上）。
> 第 1–9 步走通后，你就拿到了**第一个端到端切片**（HTTP → 图 → Text-to-SQL → 回答），后面都是在这个骨架上加能力。

| # | 模块（文档） | 依赖的前序 | 做完能验证的里程碑 |
|---|---|---|---|
| 1 | [1.config.md](1.config.md) | 无 | 配置类可加载；`ENV=prod` 缺 key 启动即失败 |
| 2 | [2.infra.md](2.infra.md) 仅开发依赖 | 1 | 在 VMware-Linux 上用 `docker compose -f infra/docker-compose.dev.yml --project-directory . up -d` 起 postgres+pgvector+redis+prometheus+grafana，Windows 侧能连（本机无 Docker，见 [2.infra-runbook.md](../runbook/2.infra-runbook.md)） |
| 3 | [3.db.md](3.db.md) | 1、2 | 8 张业务表建好 + Alembic 迁移 + 种子数据可查（向量表由 rag 后续建） |
| 4 | [4.security.md](4.security.md) | 3（白名单表名来自 schema） | 危险/越权 SQL 测试用例全绿（D1 静态层） |
| 5 | [5.llm.md](5.llm.md) 基础接入 | 1（key） | 能调通一个模型；429 会退避重试（A1 基础） |
| 6 | [6.tools.md](6.tools.md) | 4、3 | 只读查询跑通、写操作被拦、结果行数截断（C1） |
| 7 | [7.agents_module.md](7.agents_module.md) 先做 data→router→synthesis→guardrail | 5、6、4、3 | NL→SQL→校验→合成 四个 Agent 各自单测跑通（D1） |
| 8 | [8.graph.md](8.graph.md) | 7 | router→data→synthesis 有状态图跑通 |
| 9 | [9.api.md](9.api.md) | 8、4（JWT）、2（redis 限流） | **首个端到端切片**：HTTP → 图 → Text-to-SQL → 回答 ✅ |
| 10 | [10.rag.md](10.rag.md) | 3（pgvector）、5（embedding） | 知识库可检索、结果带溯源 |
| 11 | [7.agents_module.md](7.agents_module.md) 补 rag、analysis | 10、7 | router 能分流到 rag 路径；对内分析可出结论 |
| 12 | [11.observability.md](11.observability.md) | 9、10、2（prometheus/grafana） | 能从 Grafana 定位慢在哪一步（B1） |
| 13 | [5.llm.md](5.llm.md) 加固：熔断 + 多模型降级 | 5、12 | 主模型故障自动降级、用户无感（A3） |
| 14 | [12.queue.md](12.queue.md) | 8、2（redis） | 长任务不阻塞 API、失败进死信、断点重放（A1/A2） |
| 15 | [13.cache.md](13.cache.md) | 10、2（redis） | 语义缓存命中、LLM 成本下降（E1） |
| 16 | [14.eval.md](14.eval.md) | 11、12（进 CI 需 19） | 单 Agent vs 多 Agent 量化对比（G1）；CI 接入由第 19 步接通 |
| 17 | [15.mcp.md](15.mcp.md) | 6、4 | 外部 MCP 客户端能发现并调用工具 |
| 18 | [16.web.md](16.web.md) | 9、11 | 对客/对内两入口 + WebSocket 流式（B2） |
| 19 | [2.infra.md](2.infra.md) 生产部署 + CI/CD | 18、16 | 一键构建镜像并部署到 Linux（上线） |

### 两条可以「提前打草稿、后置落地」的

- **golden dataset**（第 16 步）：从第 4 步开始就可以随手记「真实问题 + 标准答案」，边开发边攒，别等到第 16 步才从零编。
- **`.env.example` / 日志规范**（第 1、2 步）：一开始就定好，否则后面每加一个模块都要回头补。

## 三条铁律

1. **每个模块开发前，先在对应文档第 6 段「开发步骤」打勾**，走一步勾一步，别跳步。
2. **每个模块开发后，用第 7 段「测试用例」跑通**，并把结果回填到第 8 段「证据链接」。
3. **每解决一条 problem-solving 条目**，回到 [`docs/problem-solving.md`](../problem-solving.md) 填 `PR#__` / `压测报告#__`。
