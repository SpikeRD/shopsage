# 模块开发与测试文档索引

> 本目录是「每个模块的开发 + 测试文档」，与 [`docs/architecture.md`](../architecture.md)（蓝图）和
> [`docs/problem-solving.md`](../problem-solving.md)（验收清单）配套使用。
> 每个模块文档统一采用 **8 段模板**，开发一个模块前先读它对应的文档，开发完按「验收标准」自检并回填证据。

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

## 模块清单与建议开发顺序

> 顺序即 `docs/architecture.md` 第 9 节的「先跑通、再加深」。**按垂直切片推进**，不要水平铺开。

| 阶段 | 模块文档 | 对应代码位置 | 承接的 problem-solving 条目 |
|---|---|---|---|
| P0 骨架 | [infra.md](infra.md) —— **仅开发依赖**：`docker-compose.dev.yml`（本机起 postgres/redis） | `infra/` | — |
| P6 部署 | [infra.md](infra.md) —— **生产部分**：`docker-compose.yml` + Dockerfile + CI/CD | `infra/`、`.github/workflows/` | — |
| P0 骨架 | [config.md](config.md) | `app/config/` | — |
| P1 数据 | [db.md](db.md) | `app/db/` | C1 |
| P1 旗舰 | [security.md](security.md) | `app/security/` | D1、C1 |
| P1 旗舰 | [agents_module.md](agents_module.md) | `app/agents/` | D1、D2 |
| P1 旗舰 | [tools.md](tools.md) | `app/tools/` | C1 |
| P2 编排 | [graph.md](graph.md) | `app/graph/` | A2 |
| P3 检索 | [rag.md](rag.md) | `app/rag/` | F1、E2 |
| P4 可靠性 | [llm.md](llm.md) | LiteLLM 接入层 | A1、A3 |
| P4 可靠性 | [queue.md](queue.md) | `app/queue/` | A1 |
| P4 可观测 | [observability.md](observability.md) | `app/observability/` | B1 |
| P5 成本 | [cache.md](cache.md) | `app/cache/` | E1、B2 |
| P6 接口 | [api.md](api.md) | `app/api/` | B2、D1 |
| P6 评测 | [eval.md](eval.md) | `eval/` | G1 |
| P6 前端 | [web.md](web.md) | `web/` | B2 |
| P6 集成 | [mcp.md](mcp.md) | `app/mcp/` | — |

## 三条铁律

1. **每个模块开发前，先在对应文档第 6 段「开发步骤」打勾**，走一步勾一步，别跳步。
2. **每个模块开发后，用第 7 段「测试用例」跑通**，并把结果回填到第 8 段「证据链接」。
3. **每解决一条 problem-solving 条目**，回到 [`docs/problem-solving.md`](../problem-solving.md) 填 `PR#__` / `压测报告#__`。
