"""rerank：对初召回结果做**二次排序**（`10.rag.md` 第 4 节第 2 条 / 第 5 节第一条 / E2）。

为什么必须有这一步（而不是「向量分高的排前面」）：

* **向量相似度 ≠ 相关度**。双塔模型（embedding）把 query 与 passage **各自**编码成向量，
  两边的交互只发生在最后那一次点积 —— 它能抓住「主题相近」，抓不住「这句话是否真的回答了问题」。
  交叉编码器（cross-encoder）把 `(query, passage)` **拼在一起**送进模型，
  每一层都在算两者的交互，判别力高一个档次，代价是**不能预计算**：
  N 条候选就要 N 次前向，所以只能用在「已经缩小到几十条」的候选集上。
* **省 token（E2）**：初召回拿 20~30 条（召回优先，宁滥勿缺），rerank 后只把 top-k（默认 5）
  送 LLM。项目的验收要求就是「token 下降且质量不降」—— 两者可以同时测出来
  （见 `scripts/rag_demo.py` 的场景 6 与 `tests/rag/test_rag_eval_report.py`）。

三种后端（`RAG_RERANK`）：

| 值 | 含义 | 用在什么时候 |
|---|---|---|
| `local` | 本地 `sentence-transformers` CrossEncoder（默认） | 正常开发/生产：零边际成本、文本不出网 |
| `api` | 兼容 `/rerank` 协议的 HTTP 端点（Jina/Cohere/vLLM 等） | 本地没有 GPU、或想用更强的商用 reranker |
| `off` | 关掉（用 RRF 融合分排序） | **对照实验**与离线单测：证明「rerank 真的带来增益」必须有个不带它的对照组 |

⚠️ **降级是显式的**：`local` 后端加载失败（没装依赖 / 权重不在本地 / 离线）时
会记一条 WARNING 并**退回 RRF 排序**，而不是抛错让整条检索路径不可用 ——
「检索没有精排」远好于「检索完全不可用」。但降级必须留下日志，
否则「rerank 坏了三周没人发现」会成为评测掉分时最难查的一类问题。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankItem:
    """待重排的一条：`key` 是调用方给的稳定标识（这里是 chunk id）。"""

    key: int
    text: str


@runtime_checkable
class Reranker(Protocol):
    """重排后端协议：给一批 (query, text) 打相关性分，**顺序与入参一致**。"""

    name: str

    def score(self, query: str, items: Sequence[RerankItem]) -> list[float]: ...


class NoopReranker:
    """不重排（`RAG_RERANK=off`）：返回全 0 分，让融合分决定顺序。

    返回 0 而不是「原样返回输入顺序的递减分」：调用方要能区分
    「rerank 给这条打了低分」与「压根没 rerank」—— 前者应当压低排名，后者应当保持融合顺序。
    """

    name = "off"

    def score(self, query: str, items: Sequence[RerankItem]) -> list[float]:
        return [0.0 for _ in items]


class CrossEncoderReranker:
    """本地 CrossEncoder（bge-reranker-base 等）。

    三个必须做对的地方（与本地 embedding 后端同一套理由，见 `5.llm-runbook.md` §7.3）：

    1. **懒加载 + 进程内单例**：模型权重约 1 GB，加载十几秒（实测 16.1s），
       绝不能每个请求加载一次，也不能在 import 期加载（会让「装了这个模块」= 「启动慢十几秒」）。
    2. **`predict` 是同步 CPU 密集**：本模块由 `asyncio.to_thread` 的调用方驱动，
       自己不做异步转换（保持「纯 CPU 逻辑」的可测试性）。
    3. **加载与推理串行化**：CrossEncoder 实例不是线程安全的，并发 `predict` 会互相踩。
       用一把模块级锁串行 —— 吞吐靠多进程解决，不靠并发同一个模型实例。
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", *, device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device
        self.name = f"local:{model_name}"
        self._model: Any = None
        self._load_error: Exception | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        with _MODEL_LOCK:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - 依赖已装时不可达
                raise RerankUnavailable(
                    "本地 rerank 需要可选依赖 sentence-transformers（含 torch，约 2 GB）："
                    "请执行 `uv sync --extra local`，或把 RAG_RERANK 切成 off/api。"
                ) from exc
            logger.info("加载本地 rerank 模型：%s（device=%s）", self.model_name, self.device)
            try:
                self._model = CrossEncoder(self.model_name, device=self.device)
            except Exception as exc:  # noqa: BLE001 - 权重缺失/离线/显存不足都走降级
                self._load_error = exc
                raise RerankUnavailable(
                    f"本地 rerank 模型加载失败（{type(exc).__name__}: {exc}）。"
                    "常见成因：权重不在本地缓存且当前离线（HF_HUB_OFFLINE=1）、模型名写错、显存/内存不足。"
                    "处置：① 联网预热一次权重；② 设 EMBEDDING_LOCAL_PATH 同款思路指向本地目录；"
                    "③ 把 RAG_RERANK 切成 off（退回 RRF 排序）。"
                ) from exc
            return self._model

    def score(self, query: str, items: Sequence[RerankItem]) -> list[float]:
        if not items:
            return []
        model = self._load()
        pairs = [(query, item.text) for item in items]
        with _MODEL_LOCK:  # 推理也要串行：实例不是线程安全的
            raw = model.predict(pairs)
        return [_sigmoid(value) for value in _as_floats(raw)]


class ApiReranker:
    """兼容 `/rerank` 协议的 HTTP 端点（Jina / Cohere / vLLM / TEI 都是这个形状）。

    为什么不用项目已有的 `litellm`：litellm 的 `rerank` 支持面随版本变化，
    而这里的协议只有「POST {model, query, documents} → results[{index, relevance_score}]」一条，
    自己写 20 行反而更稳（也避免了「rerank 走 litellm、embedding 走 litellm、但两边要求不同的 base」那种混乱）。
    请求用标准库 `urllib` 发起（同步、由调用方丢线程池）：为了一个旁路功能引入 httpx 依赖不值。
    """

    def __init__(self, base_url: str, *, model: str, api_key: str = "", timeout: float = 10.0) -> None:
        if not base_url:
            raise RerankUnavailable(
                "RAG_RERANK=api 但没有配 RAG_RERANK_API_BASE：请填兼容 /rerank 的端点基址"
                "（如 https://api.jina.ai/v1），或把 RAG_RERANK 切成 local/off。"
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.name = f"api:{model}"

    def score(self, query: str, items: Sequence[RerankItem]) -> list[float]:
        if not items:
            return []
        import json
        import urllib.error
        import urllib.request

        payload = json.dumps(
            {"model": self.model, "query": query, "documents": [item.text for item in items]}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/rerank",
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RerankUnavailable(f"rerank API 调用失败：{type(exc).__name__}: {exc}") from exc

        results = body.get("results") or body.get("data") or []
        scores = [0.0] * len(items)
        for rank, entry in enumerate(results):
            index = entry.get("index", rank)
            value = entry.get("relevance_score", entry.get("score", 0.0))
            if isinstance(index, int) and 0 <= index < len(scores):
                scores[index] = _sigmoid(float(value))
        return scores


class RerankUnavailable(RuntimeError):
    """rerank 后端不可用：调用方据此**显式降级**（记 WARNING + 退回融合分）。"""


_MODEL_LOCK = threading.Lock()


def _sigmoid(value: float) -> float:
    """把 CrossEncoder 的原始 logit 压到 (0, 1)。

    为什么必须做：bge-reranker 的原始输出是**无界**的 logit（实测 `-10` 到 `+10`），
    直接当分数用时，「0.5 分以上算相关」这类阈值就失去意义；sigmoid 之后
    `0.5` 恰好是「正负 logit 的分界」，阈值可解释、跨模型可比。
    数值稳定性：`exp(-x)` 在 x 很负时会溢出，所以按符号分两支写。
    """
    import math

    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 60.0)))
    exp_value = math.exp(max(value, -60.0))
    return exp_value / (1.0 + exp_value)


def _as_floats(raw: Any) -> list[float]:
    """把交叉编码器的返回值（可能是 numpy 数组、标量、嵌套列表）拍平成 float 列表。"""
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if not isinstance(raw, (list, tuple)):
        return [float(raw)]
    flattened: list[float] = []
    for item in raw:
        if isinstance(item, (list, tuple)):
            flattened.append(float(item[0]) if item else 0.0)
        else:
            flattened.append(float(item))
    return flattened


def build_reranker(mode: str, *, model: str, api_base: str = "", api_key: str = "", device: str = "cpu") -> Reranker:
    """按 `RAG_RERANK` 造一个重排后端（不认识的值 → 退回 `off` 并告警）。"""
    normalized = (mode or "off").strip().lower()
    if normalized == "local":
        return CrossEncoderReranker(model, device=device)
    if normalized == "api":
        return ApiReranker(api_base, model=model, api_key=api_key)
    if normalized not in ("off", ""):
        logger.warning("不认识的 RAG_RERANK=%r：按 off 处理（不重排，用融合分排序）。", mode)
    return NoopReranker()


__all__ = [
    "ApiReranker",
    "CrossEncoderReranker",
    "NoopReranker",
    "RerankItem",
    "RerankUnavailable",
    "Reranker",
    "build_reranker",
]
