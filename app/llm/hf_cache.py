"""HuggingFace 模型缓存的**环境兜底**：让「本地 embedding / rerank 后端」开箱可用。

## 为什么需要这个模块（实测踩到的坑）

本地后端（`EMBEDDING_BACKEND=local` / `RAG_RERANK=local`）在首次使用时会经
`sentence-transformers` 去 HuggingFace 取权重。**权重放哪、能不能写，由环境变量决定**，
而这两件事在本仓的开发机上都会失败：

| 现象 | 根因 |
|---|---|
| `PermissionError: [WinError 5] 拒绝访问。: 'C:\\Users\\<用户>\\.cache'` | HF 默认缓存目录是 `~/.cache/huggingface`（**仓库外**），受限环境写不进去 |
| `PermissionError: [Errno 13] ... dummy_file_src` | `HF_HOME` 改对了，但 HF 的快照目录靠**符号链接**指向 blobs；Windows 建符号链接需要**管理员权限或开发者模式** |

两者叠加的结果极具误导性：**服务能起来、`/health` 全绿**，但第一次真检索时在
`retrieve_knowledge` 里抛 `RetryableLLMError`，图降级成「只少一份材料」——
页面上照常给出一段**没有依据的**回答，看起来像「模型答得不好」，实际是环境问题。

## 本模块的职责（**只做兜底，不覆盖显式配置**）

在**加载模型之前**把两个变量摆正：

* `HF_HOME`                 → 默认 `<仓库根>/.hf-cache`（已在 `.gitignore` 里）
* `HF_HUB_DISABLE_SYMLINKS` → 默认 `1`（禁用 symlink，HF 改为复制文件，无需管理员权限）

⚠️ **显式设过的值一律不动**：运维若自己设了 `HF_HOME`（例如指到磁盘宽裕的位置），
本模块必须让路 —— 否则「缓存位置」这种运维参数就被代码写死了。这与
`app/config/settings.py` 的「环境变量 > `.env` > 代码默认值」是同一条纪律。

⚠️ **必须幂等且无副作用**：`prepare_hf_cache()` 在每次加载模型前都会被调用，
所以它自己不能有任何重活（不建目录、不探测、不打日志刷屏）。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["DEFAULT_HF_CACHE_DIR", "prepare_hf_cache"]

#: 仓库根（`app/llm/hf_cache.py` → 上两级）。
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 默认缓存目录。**刻意放在仓库内**：与 `.uv-cache` / `.npm-cache` / `.pytest-tmp` 同一套
#: 约定 —— 受限环境下只有工作区内保证可写，仓库外的用户目录一律不保证。
DEFAULT_HF_CACHE_DIR = _REPO_ROOT / ".hf-cache"

#: 环境变量名（HF 官方约定，不要改）。
_HF_HOME = "HF_HOME"
_DISABLE_SYMLINKS = "HF_HUB_DISABLE_SYMLINKS"


def prepare_hf_cache() -> None:
    """把 HF 缓存相关的环境变量摆到「本机能跑」的状态（幂等）。

    调用点：一切**即将通过 HF 加载权重**的地方，即
    `app/llm/client.py::_load_local_model` 与 `app/rag/rerank.py::CrossEncoderReranker._load`。

    只设「用户没设过的」那两个变量；重复调用不会改变任何已生效的配置。
    """
    # `setdefault` 的语义正是「用户设过就让路」，不必自己判空 —— 但空串要当成「没设」，
    # 因为 `HF_HOME=""` 在 HF 侧会被解析回默认目录（等于没设，却会让 setdefault 失效）。
    if not os.environ.get(_HF_HOME, "").strip():
        os.environ[_HF_HOME] = str(DEFAULT_HF_CACHE_DIR)
    if not os.environ.get(_DISABLE_SYMLINKS, "").strip():
        os.environ[_DISABLE_SYMLINKS] = "1"
