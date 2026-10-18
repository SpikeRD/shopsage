# KB 语料清单（`docs/kb/`）

## 目的

`docs/kb/` **不是说明书**，是 RAG 的**运行时语料**：被 `scripts/load_knowledge_base.py` 解析、分块、embedding 后灌进 pgvector，供 `rag` 节点检索。改这些文件等于**改系统的答案**。

## 关键结论

| 文件 | 大小 | 格式 | 用途 |
|---|---|---|---|
| `产品保修.md` | 1446 B | Markdown | 保修期限/条件类问答 |
| `常见问题.md` | 2049 B | Markdown | FAQ（`Q:`/`A:` 分行，走 FAQ 分块策略） |
| `会员积分.md` | 1436 B | Markdown | 积分规则 |
| `企业采购说明.txt` | 1116 B | 纯文本 | **演示 `.txt` 解析路径**（文件内明写"纯文本示例，用于演示 .txt 文档的解析与分块"） |
| `售后服务承诺.html` | 1197 B | HTML | 演示 HTML 解析路径（`app/rag/parse.py:114 parse_html`） |
| `退货政策.md` | 2024 B | Markdown | 退货核心政策；`issues/010` 的复现样本；**2026-10-17 被改过**（用于验证"改文档 → 版本推进"） |
| `物流政策.md` | 1430 B | Markdown | 物流时效 |
| `账户安全与隐私.md` | 1558 B | Markdown | 隐私与账号安全 |

共 **8 份**文档、4 种格式（`.md` ×6、`.txt`、`.html`）。`app/rag/parse.py:35` 还支持 `.pdf`（`pypdf`，在 dev 依赖组），但当前语料里没有 PDF 样本。

## 待办 / 已知边界

- ⚠️ **片段数（文档多处写 61）本次未复核** —— 需要真跑灌库（`uv run python -m scripts.load_knowledge_base`）才能确认；分块参数变更后这个数会变。→ `../03-待确认清单.md` Q-07。
- ⚠️ `退货政策.md` 在 2026-10-17 16:50 被修改过，修改原因是验证 `corpus_revision` 推进（`issues/010` 的实测步骤之一）。它当前的内容**是否为产品最终版**，本次无从判断 —— 属"存疑保留"（`../appendix/存疑保留.md`）。
- 语料与 `docs/changes/003` 的退款流程存在**潜在口径冲突**：语料里的退货政策是静态文本，实际退款额度由 `app/tools/refund.py` 按订单金额动态算。若两者不一致，客户会看到"政策说能退、系统说不能退" —— ⚠️ 本次未能逐条比对 → `../03-待确认清单.md` Q-12。

## 来源

- `docs/kb/`（8 个文件）
- `app/rag/parse.py`、`app/rag/chunking.py`、`scripts/load_knowledge_base.py`
