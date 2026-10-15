# 002 · HF 模型缓存写在仓库外 → 首次真检索抛 PermissionError（有误导性）

* **状态**：已修复（2026-10-15）
* **发现于**：2026-10-15，新机器环境迁移验收
* **归属模块**：第 5 步本地 embedding / 第 10 步本地 rerank
* **修复位置**：[`app/llm/hf_cache.py`](../app/llm/hf_cache.py)（新增）、
  [`app/llm/client.py`](../app/llm/client.py)、[`app/rag/rerank.py`](../app/rag/rerank.py)
* **相关文档**：[`docs/环境迁移验收.md`](../docs/环境迁移验收.md) §2 第 9 步

---

## 现象

服务**能正常启动**，`/health` 五项全 `ok`，单元测试也全绿。
但**第一次真的走检索**时（对客入口问知识库问题）：

```text
Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN ...

tool_error {"tool": "retrieve_knowledge", "caller": "agent:rag",
  "outcome": "error", "error_type": "RetryableLLMError",
  "details": {"error": "LLM 重试 3 次后仍失败：PermissionError: [WinError 5] 拒绝访问。: 'C:\\Users\\<用户>\\.cache'"}}

执行在 rag 失败 → 从断点重放（已完成的节点不再执行）
图级兜底：知识库检索这一步没做成 → 继续（只少一份材料）
```

界面上表现为：**照常给出一段回答，但没有任何出处**（是兜底话术），
看起来像「模型答得不好」，实际是环境问题。

设了 `HF_HOME` 之后还会撞上**第二个**报错：

```text
PermissionError: [Errno 13] Permission denied: '...\\.hf-cache\\hub\\models--BAAI--bge-m3\\
  snapshots\\<hash>\\tmp<xxx>\\dummy_file_src'
```

---

## 影响

* **会误判**：`/health` 全绿 + 单测全过，让人以为环境没问题；
  失败发生在**运行时**且被图**降级吞掉**（`只少一份材料`），
  **不中断请求**，只让答案悄悄失去依据 —— 这是最危险的一种失败。
* 排查方向容易被带偏到「模型质量」「prompt」「检索算法」，而真因是**缓存目录不可写**。

---

## 根因（两层，各有各的坑）

| # | 报错 | 根因 |
|---|---|---|
| ① | `PermissionError: [WinError 5] ... 'C:\\Users\\<用户>\\.cache'` | HuggingFace 默认缓存目录是 `~/.cache/huggingface`，**在仓库之外**；本机受限环境只保证工作区内可写 |
| ② | `PermissionError: [Errno 13] ... dummy_file_src` | `HF_HOME` 改对后，HF 的 `snapshots/` 目录靠**符号链接**指向 `blobs/`，而 **Windows 建符号链接需要管理员权限或开发者模式**（实测本机两者皆无，`New-Item -ItemType SymbolicLink` 报「此操作需要管理员权限」） |

### 一个反直觉的点（第一版修复**无效**的原因）

`huggingface_hub` 的 `HF_HOME` / `HF_HUB_CACHE` 是在**模块导入时**读成常量固化的，
**之后再设环境变量不生效**。而 `import litellm` 会连带导入 `litellm.llms.huggingface.*` ——
于是常量在 `app/llm/client.py` 的 `import litellm` 那一行就定死了。

> 实测：把兜底放在 `_load_local_model()` **函数体内**（懒加载处）**完全无效**，仍去写 `~/.cache`；
> 必须抢在 `import litellm` **之前**执行。

---

## 判据

| 观察 | 判定 |
|---|---|
| `/health` 全绿 **且** 问知识库问题**带出处** | 正常 |
| 问知识库问题**没有出处**、日志里有 `PermissionError: ~/.cache` 或 `dummy_file_src` | 本文这条问题（缓存目录不可写） |
| 日志里是 `litellm.Timeout ... keep-alive` 且零内容 chunk | **另一回事**（推理上游不产出，见环境迁移验收文档第 10 步） |

---

## 处理（已修复）

新增 `app/llm/hf_cache.py`，在加载模型前自动摆好环境：

* `HF_HOME` → 默认 `<仓库根>/.hf-cache`（已在 `.gitignore` 里）
* `HF_HUB_DISABLE_SYMLINKS` → 默认 `1`（HF 改为复制文件，无需管理员权限）

**两个加载点都接**（只修一处，另一处照炸）：

| 加载点 | 加载什么 |
|---|---|
| `app/llm/client.py::_load_local_model` | 本地 embedding（`BAAI/bge-m3`） |
| `app/rag/rerank.py::CrossEncoderReranker._load` | 本地 reranker（`BAAI/bge-reranker-base`） |

**行为约定**：幂等；**显式设过的环境变量优先**（运维想换缓存位置仍可覆盖）；
空串当成「没设」；`hf_cache.py` 只 import `os`/`pathlib`，无循环依赖。

## 验证证据（2026-10-15 实测）

| 验证 | 结果 |
|---|---|
| 零 HF 环境变量下加载 embedding | ✅ 维度 1024 |
| 零 HF 环境变量下加载 reranker | ✅ 出分，退货条款 0.5766 > 物流条款 0.5014（排序正确） |
| 零 HF 环境变量起后端 → 问 `退货政策是怎么规定的？` | ✅ `citations=5`、`flow=delivered`（修复前：`PermissionError` + 图降级） |
| 真链路用例 | ✅ 10/10 通过 |
| 全量回归 | 1627 passed / 1 failed / 6 errors（与修复前逐条一致，无新增） |
