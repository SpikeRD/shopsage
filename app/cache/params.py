"""**检索参数的唯一构造处**（`params` 映射里该放什么，只在这一个函数里决定）。

## 为什么需要它（本步最贵的一个坑）

缓存条的版本指纹 = 知识库版本 + prompt 版本 + embedding 模型 + **检索参数**。
最后一项是「用哪几个参数查出来的答案」的标识 —— 它必须**读写两侧完全一致**。

第一版把这件事交给调用方「顺手传进去」，结果是：

* **读路径**（`rag_node`）传了 `top_k` / `min_score` / `embedder_key`；
* **写路径**（`synthesis`）只传了 `top_k`，还多塞了一个记账标志 `llm_calls_saved`。

两个指纹必然不同 → 第一次问答写进缓存，第二次问同一个问题**仍然未命中**，
而两边的代码各自看起来都对。实测发现它的方式是
`tests/cache/test_cache_graph.py::test_second_turn_hits_and_skips_retrieval_and_model`
（那条用例的失败信息是「命中时不该再检索」）。

所以现在有一条纪律：**「指纹里放什么」由一个函数说了算**，两处都调它。

## 判据：这个参数变了，答案会变吗？

| 参数 | 进指纹？ | 为什么 |
|---|---|---|
| `top_k` | **是** | 取 5 条与取 20 条生成出的答案不同 |
| `min_score` | **是** | 阈值变了，材料就变了 |
| `embedder_key` | **是** | 它是「检索后端是谁」的标识（假后端/真后端、不同索引） |
| `rerank` / `rerank_model` | 目前**否** | 它们已经通过 `embedder_key`（后端实例）与 `PROMPT_VERSION` 反映；单独加会多一个「改了就全量失效」的旋钮，而现在还没有那个需求 |
| `llm_calls_saved` / `route` / 耗时 | **否** | 它们是**记账**，不影响答案 —— 把它们算进去就是第一版那个 bug |

⚠️ `embedder_key` 用 `str(search)`（对象标识）而不是「后端类型名」：
两个不同的后端实例（一个连测试索引、一个连生产索引）必须被区分开，
而它们的类型名是一样的。代价是「同一个后端对象在两次请求里是同一个」这条前提 ——
进程内它是成立的（后端是注入进来的、按进程构造一次）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["PARAM_KEYS", "describe", "retrieval_params"]

#: 允许进指纹的参数名（**白名单**：多传一个就抛错，而不是静默塞进去）。
PARAM_KEYS: tuple[str, ...] = ("top_k", "min_score", "embedder_key")


def retrieval_params(
    *,
    top_k: Any = None,
    min_score: Any = None,
    search: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """造「这一次检索用了哪几个参数」的映射（**读写两侧都调它**）。

    `search`（检索后端对象）会被转成 `embedder_key=str(search)` ——
    只在它非 `None` 时才进（生产路径上 `search=None` 表示「走默认后端」，
    而默认后端在两次请求里是同一个对象，不必也不该进指纹）。
    """
    params: dict[str, Any] = {"top_k": top_k, "min_score": min_score}
    if search is not None:
        params["embedder_key"] = str(search)
    for key, value in dict(extra or {}).items():
        if key not in PARAM_KEYS:
            raise ValueError(
                f"检索参数 {key!r} 不在白名单 {PARAM_KEYS} 里。"
                "它会被算进版本指纹，而指纹里只允许放「会改变答案」的量 ——"
                "记账类的东西（省了几次调用、耗时、route）放进去会让读写两侧算出不同指纹，"
                "于是缓存写了也命中不了（见 app/cache/params.py 的说明）。"
            )
        params[key] = value
    return params


def describe(params: Mapping[str, Any] | None) -> str:
    """一句话描述（日志与演示脚本用；**不打印对象地址**）。"""
    items = []
    for key in sorted(params or {}):
        value = (params or {})[key]
        if value is None or value == "":
            continue
        if key == "embedder_key":
            # `str(search)` 里带对象地址（每次运行都不同），打出来只会让日志更难读
            value = "custom"
        items.append(f"{key}={value}")
    return "、".join(items) or "（默认参数）"
