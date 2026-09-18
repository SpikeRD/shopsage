# ShopSage — 电商企业智能助手

接入企业数据库（用户/订单/商品数据）与内部知识库的多 Agent 协作系统，同时服务「客户问题处理」与「内部员工需求」，支持 Docker 容器化部署到 Linux 服务器。

## 文档导航

| 文档 | 内容 | 建议阅读时机 |
|---|---|---|
| [docs/architecture.md](docs/architecture.md) | 技术框架、模块划分、多 Agent 协作、数据库 Schema、安全/可观测/可伸缩设计、开发部署流程 | 先读，建立整体认知 |
| [docs/problem-solving.md](docs/problem-solving.md) | 痛点 → 方案 → 技术点 → 指标的工程问题清单（含落地优先级） | 后读，用于落地与面试/简历讲述 |
| [docs/config-guide.md](docs/config-guide.md) | **配置实操手册**：配置从哪取值、怎么改（`.env` / 环境变量）、Docker 怎么传值、`test_config.py` 怎么手动测 | 第一次配环境时读，排查「配置不生效」时查 |
| [docs/modules/](docs/modules/README.md) | 每个模块的开发 + 测试文档（8 段模板），含建议开发顺序与 problem-solving 映射 | 开发每个模块前/后，边做边查 |

## 建议阅读顺序

1. **architecture.md**：了解技术栈选型与模块边界，明确「对客/对内」两条业务线。
2. **problem-solving.md**：按 P0 → P1 → P2 优先级，把架构中的设计落到可验证的工程问题，形成「问题 → 方案 → 证据」闭环。
3. **docs/modules/**：开始写某个模块前，读它对应的「开发 + 测试」文档；开发完按「验收标准」自检并回填证据。
