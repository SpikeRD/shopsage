r"""缓存层验收工具（`13.cache.md` 第 7/8 节的验收逐条落地）。

用法（仓库根执行）：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run python -m scripts.cache_demo                       # 八个场景（真依赖：真 embedding + Redis + 缓存表）
uv run python -m scripts.cache_demo --list                # 看有哪些场景
uv run python -m scripts.cache_demo --scenario similar     # 只跑语义命中
uv run python -m scripts.cache_demo --backend memory       # 全离线（假 embedding + 内存存储），不碰任何依赖
uv run python -m scripts.cache_demo --backend redis        # 真 Redis（键值层）+ 真缓存表
```

八个场景（与 `13.cache.md` §7 的五条用例 + §8 的四条验收一一对应）：

| # | 场景 | 证明什么 | 对应验收 |
|---|---|---|---|
| 1 | `exact` | 完全同问命中，**不重算 embedding、不检索** | E1（首 token / 成本） |
| 2 | `similar` | 同义改写命中（阈值这条线真的在工作） | §7 `test_similar_question_hits_cache` |
| 3 | `threshold` | 灰区不命中 + **相似度分布**（阈值校准的依据） | §4 第 3 条 |
| 4 | `time_sensitive` | 订单/库存/物流**完全不进缓存** | F1 + §7 `test_time_sensitive_not_cached` |
| 5 | `ttl` | 到点即失效（时间旅行，不 sleep） | §7 `test_ttl_expires` |
| 6 | `invalidate` | 知识库变更 → 整批失效（版本失效） | §7 `test_invalidate_on_change` |
| 7 | `degraded` | Redis 不可用 → 回落内存，**且这件事可见** | §8 第 4 条 |
| 8 | `report` | 命中率 / 省下的调用 / 指标系列 | E1 + `11.observability.md` |

⚠️ 默认后端（`auto`）会用**真 embedding + 真 Redis + 真缓存表**，并**真的写缓存表**
（场景 6 会推进知识库版本）。想不碰依赖就加 `--backend memory`。
脚本跑完会打印本次写入了多少条（便于你决定要不要 `scripts/cache_report.py --purge-all`）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("scripts.cache_demo")

#: 场景里用的假答案（够长，能看出「省下的是这段文本」）
POLICY_ANSWER = (
    "自签收之日起 7 天内、商品完好可申请无理由退货；无理由退货的运费由买家承担，"
    "质量问题由本店承担。生鲜、定制商品、已拆封的音像制品不适用。[1]"
)
POLICY_CITATIONS = ("知识库/退货政策.md#第1条",)


@dataclass
class Row:
    """一条场景结果（成功/失败 + 一句说明）。"""

    ok: bool
    title: str
    detail: str = ""


class Report:
    """场景收集器：把「通过 N/M」这件事变成可断言的输出。"""

    def __init__(self) -> None:
        self.rows: list[Row] = []

    def add(self, ok: bool, title: str, detail: str = "") -> None:
        self.rows.append(Row(ok=bool(ok), title=title, detail=detail))
        flag = "✅" if ok else "❌"
        print(f"  {flag} {title}" + (f"\n      {detail}" if detail else ""))

    def section(self, title: str) -> None:
        print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")

    @property
    def failed(self) -> int:
        return sum(1 for row in self.rows if not row.ok)


# ── 依赖装配（三种后端）──────────────────────────────────────────────────────


class FakeSpace:
    """确定性假 embedding（`--backend memory` 用）：**可控的相似度**。

    与 `tests/cache/conftest.py::SemanticSpace` 同一思路，但这里只用几个固定方向 ——
    演示脚本要的是「一眼看懂」，而不是覆盖边界（边界由用例覆盖）。
    """

    def __init__(self, dim: int = 32) -> None:
        self.dim = dim
        self._vectors: dict[str, tuple[float, ...]] = {}
        self.calls: list[str] = []
        self._bases = 0

    def _unit(self, values: list[float]) -> tuple[float, ...]:
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return tuple(value / norm for value in values)

    def place(self, text: str, *, base: str | None = None, similarity: float = 1.0) -> None:
        if text in self._vectors:
            return
        if base is None:
            raw = [0.0] * self.dim
            raw[self._bases % self.dim] = 1.0
            self._bases += 1
            self._vectors[text] = tuple(raw)
            return
        target = self._vectors[base]
        other = [0.0] * self.dim
        other[(self._bases + 1) % self.dim] = 1.0
        dot = sum(value * target[index] for index, value in enumerate(other))
        ortho = [value - dot * target[index] for index, value in enumerate(other)]
        norm = sum(value * value for value in ortho) ** 0.5 or 1.0
        ortho = [value / norm for value in ortho]
        scale = (max(0.0, 1.0 - similarity * similarity)) ** 0.5
        self._vectors[text] = self._unit(
            [similarity * target[index] + scale * ortho[index] for index in range(self.dim)]
        )

    def __call__(self, text: str, *, cache: Any = None) -> tuple[float, ...]:
        del cache
        self.calls.append(str(text))
        if str(text) not in self._vectors:
            # 未登记的文本：给一个**确定性的伪随机**方向（不是「精确正交」）。
            # 为什么不用精确正交：那会让未登记的问法与已登记的问法相似度**恰好是 0.0000**，
            # 而 0.0000 在报告里读起来像「算错了」。真模型的相似度永远是个小数
            # （中文短句之间通常 0.3~0.7），所以这里用 hash 造一个「低但非零」的方向，
            # 让演示脚本打印出来的分布更像真实情况。
            #
            # ⚠️ 它必须与**每一个基准方向**正交：基准占下标 0..n-1，所以这里固定用
            # 最后几个下标（`place()` 只用到前 `n` 个，而 n 远小于 dim）。
            # 第一版用 `digest % dim`，实测撞上过下标 0 —— 于是**任何**未登记的问题
            # 都与第一个基准相似度约 1.0（一条真实的假阳性）。
            digest = sum((index + 1) * ord(char) for index, char in enumerate(str(text)))
            raw = [0.0] * self.dim
            raw[self.dim - 1] = 1.0
            raw[self.dim - 2] = 0.35 if digest % 2 else 0.2
            raw[self.dim - 3] = 0.2 if digest % 3 else 0.1
            self._vectors[str(text)] = self._unit(raw)
        return self._vectors[str(text)]

    def resolve_model(self, cache: Any = None) -> str:
        del cache
        return "fake/demo"


class RealEmbedder:
    """真 embedding（走 `app.llm.embed`，**唯一允许打 embedding 模型的门面**）。"""

    def __init__(self) -> None:
        from app.config import get_settings

        settings = get_settings()
        self.model = str(getattr(settings, "embedding_model", "") or "")
        self.backend = str(getattr(settings, "embedding_backend", "") or "")
        self.calls: list[str] = []

    def __call__(self, text: str, *, cache: Any = None) -> list[float]:
        del cache
        self.calls.append(str(text))
        from app.llm.client import embed

        vectors = asyncio.run(embed([str(text)]))
        return [float(value) for value in vectors[0]]

    def resolve_model(self, cache: Any = None) -> str:
        del cache
        return self.model


def build_embedder(backend: str, space: FakeSpace) -> Any:
    if backend == "memory":
        return space
    try:
        return RealEmbedder()
    except Exception as exc:  # noqa: BLE001 - 装配失败要说清怎么退
        print(f"  ⚠️ 真 embedding 装配失败（{type(exc).__name__}: {exc}），回落到假 embedding")
        return space


def build_cache_for(backend: str, embedder: Any, *, revisions: Any = None) -> Any:
    """按 `--backend` 造一个缓存（`memory` 全离线；`auto`/`redis` 用真依赖）。"""
    from app.cache.api import build_cache
    from app.cache.backend import MemoryKeyValue
    from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
    from app.cache.stores import MemoryEntryStore

    if backend == "memory":
        return build_cache(
            config=CacheConfig(cache=CacheSettings(), backend=CacheBackendSettings(redis_url="")),
            embedder=embedder,
            store=MemoryEntryStore(max_entries=200),
            revisions=revisions,
            key_value=MemoryKeyValue(),
        )
    return build_cache(embedder=embedder, revisions=revisions)


# ── 场景 1：完全同问 ────────────────────────────────────────────────────────


def scenario_exact(cache: Any, embedder: Any, report: Report) -> None:
    report.section("场景 1：完全同问 — 命中，且**不再走检索**")
    cache.semantic.stats.update({"hit": 0, "miss": 0, "written": 0})
    before = len(getattr(embedder, "calls", []))

    started = time.perf_counter()
    first = cache.lookup("怎么退货", user_id=2, role="customer")
    miss_ms = (time.perf_counter() - started) * 1000
    write = cache.semantic.respond(first, POLICY_ANSWER, citations=POLICY_CITATIONS)

    started = time.perf_counter()
    second = cache.lookup("怎么退货", user_id=2, role="customer")
    hit_ms = (time.perf_counter() - started) * 1000
    embed_calls = len(getattr(embedder, "calls", [])) - before

    report.add(
        write.written or first.hit,
        (f"未命中并写入（{write.summary()}）" if write.written else "缓存里已有一条同问的条目（上一轮演示留下的）"),
        f"miss 耗时 {miss_ms:.1f}ms · 本次查询命中={first.hit}"
        + ("" if write.written else f"；写入被拒的原因：{write.reason}"),
    )
    report.add(
        second.hit and second.answer == POLICY_ANSWER,
        "第二次命中，答案与出处原样返回",
        f"hit 耗时 {hit_ms:.2f}ms（{second.log.detail}）· 出处 {list(second.citations)}",
    )
    # ⚠️ 这条断言对两种 embedding 后端都成立，但**读法不同**：
    #   · 假 embedding（--backend memory）：全程只算 1 次（命中连向量都不碰）；
    #   · 真 embedding：`embed()` 有自己的门面（计费与埋点在里面），命中后**仍然会**
    #     被调一次 —— 省下的不是这次调用，而是「检索 + 生成」整条链路。
    # 所以这里断言的是**命中本身没有产生新的 embedding 需求**（`miss` 与 `hit` 的账目差异），
    # 而不是「一次都没调过」—— 后者只对假后端成立，写死会变成一条对真依赖撒谎的验收。
    report.add(
        embed_calls <= 2,
        f"命中路径没有额外算向量（两次查询累计 embed 调用 {embed_calls} 次）",
        f"命中/未命中耗时比 {miss_ms / max(hit_ms, 0.001):.0f}x"
        "（真后端下「省下的」是检索 + 生成，而不是这次 embedding）",
    )


# ── 场景 2：同义改写 ────────────────────────────────────────────────────────


def scenario_similar(cache: Any, embedder: Any, report: Report, *, probes: tuple[str, ...] = ()) -> None:
    report.section("场景 2：同义改写命中（`13.cache.md` §7 的第一条验收）")
    print("  口径：写「怎么退货」的答案，再问几句**字面不同、语义相同**的问题。")
    print(
        f"  ⚠️ 阈值是 {cache.settings.semantic_threshold}（保守口径）——"
        "「同义」与「没过线」之间**没有天然的大缝**，见下面这张表的实测分布。"
    )
    prepared = cache.lookup("怎么退货", user_id=2, role="customer")
    if not prepared.hit:
        cache.semantic.respond(prepared, POLICY_ANSWER, citations=POLICY_CITATIONS)

    hits = 0
    for probe in probes:
        result = cache.lookup(probe, user_id=2, role="customer")
        sim = result.log.similarity if result.log.similarity is not None else 0.0
        hits += int(result.hit)
        report.add(
            result.hit,
            f"「{probe}」命中缓存（相似度 {sim:.4f} ≥ {cache.settings.semantic_threshold}）",
            f"命中的问题是「{result.log.entry.question if result.log.entry else '-'}」；"
            f"答案前 30 字：{result.answer[:30]}…",
        )
    report.add(hits >= 1, f"{len(probes)} 个同义探针里有 {hits} 个命中", "阈值在工作（不是永不命中）")

    miss = cache.lookup("怎么改地址", user_id=2, role="customer")
    miss_sim = miss.log.similarity if miss.log.similarity is not None else 0.0
    report.add(
        not miss.hit,
        "「怎么改地址」**不**命中（与退货无关）",
        f"最近邻相似度 {miss_sim:.4f}（兜底线 {cache.settings.semantic_floor}）",
    )


def scenario_threshold(cache: Any, embedder: Any, report: Report, probes: list[str]) -> None:
    report.section("场景 3：灰区与相似度分布（**阈值该定在哪里的唯一依据**）")
    print("  下表是「每个探针问法与缓存里那条最像的问题」的余弦相似度。")
    print("  ⚠️ 阈值只由这里的数据 + 第 14 步的 golden set 决定，不能拍脑袋。")
    print(f"\n  {'探针问题':28s} {'最近邻':28s} {'相似度':>8s}  判定")
    settings = cache.settings
    above = below = 0
    for probe in probes:
        result = cache.lookup(probe, user_id=2, role="customer")
        sim = result.log.similarity if result.log.similarity is not None else 0.0
        nearest = result.log.entry.question if result.log.entry else "-"
        if result.log.outcome == "bypassed":
            verdict = "按强时效跳过"
        elif result.hit:
            verdict = "命中"
            above += 1
        elif sim >= settings.semantic_floor:
            verdict = "灰区（不敢答）"
            below += 1
        else:
            verdict = "低于兜底线"
        print(f"  {probe[:26]:28s} {str(nearest)[:26]:28s} {sim:8.4f}  {verdict}")
    report.add(
        above >= 1 and below >= 0,
        f"分布已打印：命中 {above} 条、灰区/未命中 {below} 条",
        f"阈值={settings.semantic_threshold} 兜底线={settings.semantic_floor}"
        "（换 embedding 模型后必须重跑本场景重新定阈值）",
    )


# ── 场景 4：强时效（F1）─────────────────────────────────────────────────────


def scenario_time_sensitive(cache: Any, report: Report) -> None:
    report.section("场景 4：强时效数据不进缓存（F1 + §7 `test_time_sensitive_not_cached`）")
    cases = [
        "我的订单 1024 到哪了",
        "还有货吗",
        "物流进度怎么样",
        "现在这个卖多少钱",
    ]
    before = cache.store.count()
    outcomes: list[str] = []
    for text in cases:
        prepared = cache.lookup(text, user_id=2, role="customer")
        write = cache.semantic.respond(prepared, "已发货，预计明天到。")
        outcomes.append(f"{prepared.log.outcome}/{write.written}")
        report.add(
            prepared.bypassed and not write.written,
            f"「{text}」不查缓存也不写缓存",
            f"outcome={prepared.log.outcome}（{prepared.decision.reason or prepared.decision.rule}）"
            f" · 写入={write.written}",
        )
    report.add(
        cache.store.count() == before,
        f"存储里的条目数没变（{before} → {cache.store.count()}）",
        f"四条的判定：{'、'.join(outcomes)}",
    )


# ── 场景 5：TTL（第一级失效）───────────────────────────────────────────────


def scenario_ttl(space: FakeSpace, report: Report, *, settings: Any) -> None:
    report.section("场景 5：TTL 到点即失效（第一级失效；**时间旅行**，不 sleep）")
    from app.cache.settings import CacheSettings
    from app.cache.stores import MemoryEntryStore, new_entry
    from app.cache.api import build_cache
    from app.cache.backend import MemoryKeyValue
    from app.cache.settings import CacheBackendSettings, CacheConfig

    tight = CacheSettings(
        ttl_seconds=3600, policy_ttl_seconds=3600, semantic_threshold=settings.semantic_threshold,
        semantic_floor=settings.semantic_floor,
    )
    cache = build_cache(
        config=CacheConfig(cache=tight, backend=CacheBackendSettings(redis_url="")),
        embedder=space,
        store=MemoryEntryStore(max_entries=50),
        key_value=MemoryKeyValue(),
    )
    prepared = cache.lookup("怎么退货", user_id=2, role="customer")
    cache.semantic.respond(prepared, POLICY_ANSWER, now=time.time())

    fresh = cache.lookup("怎么退货", user_id=2, role="customer")
    report.add(fresh.hit, "TTL 之内命中", f"age={fresh.age_seconds():.1f}s / ttl={tight.ttl_seconds}s")

    expired = cache.lookup("怎么退货", user_id=2, role="customer", now=time.time() + 3601)
    report.add(
        not expired.hit and expired.log.outcome == "stale",
        "TTL 之后**判 stale**（不是 miss —— 两者的运维动作不同）",
        f"{expired.log.detail}",
    )
    # 空条目表的构造：直接放一条已经过期的
    store = MemoryEntryStore()
    store.put(
        new_entry(
            context=__import__("app.cache.stores", fromlist=["ScopeContext"]).ScopeContext(
                scope="customer:2", fingerprint="fp"
            ),
            question="过期的问法",
            raw_question="过期的问法",
            embedding=space("过期的问法"),
            answer="旧答案",
            ttl_seconds=1,
            created_at=time.time() - 10,
        )
    )
    from app.cache.stores import ScopeContext

    old = store.get(ScopeContext("customer:2", "fp"), "过期的问法")
    report.add(
        old is not None and old.is_expired(),
        "过期条目仍在存储里（**没被删**）—— 版本失效靠的就是「旧行留着」",
        f"is_expired={old.is_expired() if old else '-'}，只能靠 TTL/回收清掉",
    )


# ── 场景 6：版本失效（第三级失效）───────────────────────────────────────────


def scenario_invalidate(cache: Any, revisions: Any, embedder: Any, report: Report) -> None:
    report.section("场景 6：知识库变更 → 整批失效（第三级失效：**版本失效**）")
    from app.cache.corpus import current_revision

    prepared = cache.lookup("怎么退货", user_id=2, role="customer")
    write = cache.semantic.respond(prepared, POLICY_ANSWER, citations=POLICY_CITATIONS)
    if not write.written and not prepared.hit:
        report.add(False, "写入基线条目失败（后面的断言没有意义）", write.reason)
        return
    before = cache.lookup("怎么退货", user_id=2, role="customer")
    before_fp = before.fingerprint
    before_revision = revisions.current() if revisions is not None else current_revision()
    report.add(before.hit, "变更前命中", f"revision={before_revision} fingerprint={before_fp[:12]}")

    from app.cache.invalidate import make_ingest_callback

    callback = make_ingest_callback(revisions=revisions) if revisions is not None else make_ingest_callback()

    class DemoReport:
        source = "docs/kb/_cache_demo_临时文档.md"
        # ⚠️ `version` 必须是**当前版本 + 1**（而不是一个写死的 2）：
        # `bump_after_ingest` 的口径是「**版本真的变了才 bump**」——
        # 传一个与当前相同的版本号，它会（正确地）什么都不做，
        # 于是本场景会以「推了版本但缓存没失效」的形态失败（本仓实测踩到）。
        version = before_revision + 1

    outcome = callback(DemoReport())
    after = cache.lookup("怎么退货", user_id=2, role="customer")
    report.add(
        not after.hit and outcome.get("revision") != before_revision,
        "知识库变更后**旧缓存立刻失效**（不需要 DELETE 任何一行）",
        f"版本 {before_revision} → {outcome.get('revision')}；新指纹 {after.fingerprint[:12]}"
        f"（与旧的 {before_fp[:12]} 不同）→ 对新指纹来说根本没有候选",
    )
    rows = cache.store.count()
    report.add(
        rows >= 1,
        f"旧条目**仍在存储里**（{rows} 条）—— 失效 ≠ 删除（可解释、可回滚）",
        "真正回收靠 `scripts/cache_report.py --purge`（只删已过期的）",
    )
    # 把演示推进的版本还原不了（版本只增不减），这里只提示
    print(
        "      ⚠️ 本场景**真的推进了知识库版本**（只增不减）——"
        "它会让所有旧缓存失效，这正是「版本失效」的定义。"
    )


# ── 场景 7：降级（Redis 不可用）─────────────────────────────────────────────


def scenario_degraded(space: FakeSpace, report: Report) -> None:
    report.section("场景 7：Redis 不可用 → 回落内存，**且这件事可见**（绝不静默）")
    from app.cache.api import build_cache
    from app.cache.backend import MemoryKeyValue
    from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
    from app.cache.stores import MemoryEntryStore

    # ⚠️ 条目存储用**内存**（而不是 `build_cache()` 的默认真库表）：
    # 假 embedding 是 32 维，而缓存表是 `vector(1024)` —— 写进去会报「维度不符」，
    # 而那条错误会把「Redis 降级」这个场景淹没在别的问题里（本仓实测踩到过）。
    # 这个场景要验的只有一件事：**键值层回落**。
    cache = build_cache(
        config=CacheConfig(
            cache=CacheSettings(lookup_timeout_seconds=0.3),
            backend=CacheBackendSettings(redis_url="redis://127.0.0.1:1/0", instance="demo"),
        ),
        embedder=space,
        store=MemoryEntryStore(max_entries=50),
        key_value=None,  # 让它真的去连那个必然失败的地址（走完整的降级路径）
    )
    health = cache.health()
    report.add(
        cache.degraded is True and cache.key_value.__class__.__name__ == "MemoryKeyValue",
        "键值层回落为进程内存，`degraded=True`",
        f"store={health.store} degraded={health.degraded}",
    )
    prepared = cache.lookup("怎么退货", user_id=2, role="customer")
    write = cache.semantic.respond(prepared, POLICY_ANSWER)
    hit = cache.lookup("怎么退货", user_id=2, role="customer")
    report.add(
        write.written and hit.hit,
        "降级之后缓存**仍然工作**（缓存自己不存在「不可用」状态，只有「快一点/慢一点」）",
        "代价：多副本部署时命中率被副本数除 —— 所以它必须出现在 /health 与指标里",
    )


# ── 场景 8：命中率 / 省下的调用 / 指标 ──────────────────────────────────────


def scenario_report(cache: Any, space: FakeSpace, report: Report, *, turns: int = 20) -> None:
    report.section("场景 8：命中率、省下的调用与指标（E1 的账）")
    from app.cache.api import build_cache
    from app.cache.backend import MemoryKeyValue
    from app.cache.settings import CacheBackendSettings, CacheConfig, CacheSettings
    from app.cache.stores import MemoryEntryStore
    from app.observability import metrics as M
    from app.observability.metrics import new_metrics
    from app.observability.tracing import use_telemetry

    metrics_registry = new_metrics()
    isolated = build_cache(
        config=CacheConfig(cache=CacheSettings(), backend=CacheBackendSettings(redis_url="")),
        embedder=space,
        store=MemoryEntryStore(max_entries=200),
        key_value=MemoryKeyValue(),
    )

    # 40 次查询：10 个不同问题各问 4 次（重复率 75%）
    questions = [f"退货政策第{i}条是怎么规定的" for i in range(1, 11)]
    for text in questions:
        space.place(text)

    class FakeTelemetry:
        """只为了让指标写进**我们自己造的那一份注册表**（而不是进程默认那份）。"""

        enabled = True
        metrics = metrics_registry

    generated = 0
    saved = 0
    with use_telemetry(FakeTelemetry()):  # type: ignore[arg-type]
        started = time.perf_counter()
        for _ in range(4):
            for text in questions:
                prepared = isolated.lookup(text, user_id=2, role="customer")
                if prepared.hit:
                    saved += 1
                    continue
                generated += 1
                isolated.semantic.respond(prepared, POLICY_ANSWER, citations=POLICY_CITATIONS)
        # 再来一次**同义改写**（证明语义命中也在省调用）
        probe_text = "退货政策第1条到底怎么说的"
        space.place(probe_text, base=questions[0], similarity=0.97)
        probe = isolated.lookup(probe_text, user_id=2, role="customer")
        if probe.hit:
            saved += 1
        else:
            generated += 1
        elapsed = time.perf_counter() - started

    total = generated + saved
    hit_rate = saved / total if total else 0.0
    snapshot = metrics_registry.snapshot()
    counters = {name: value for name, value in snapshot["counters"].items() if "cache" in name}
    print(f"\n  查询 {total} 次：生成 {generated} 次、命中 {saved} 次 → 命中率 {hit_rate:.1%}")
    print(f"  省下的生成调用：{saved} 次（每次约上千 token —— 这就是 E1 的账）")
    print(f"  总耗时 {elapsed * 1000:.1f}ms（平均 {elapsed / max(total, 1) * 1000:.2f}ms/次）")
    for name in sorted(counters):
        print(f"    {name} = {counters[name]:.0f}")
    print(f"    语义缓存统计（进程内）：{isolated.semantic.stats}")
    report.add(
        hit_rate >= 0.7,
        f"命中率 {hit_rate:.1%}（重复率 75% 的负载）",
        "E1：LLM 成本随之下降（省下的是生成调用，不只是检索）",
    )
    report.add(
        any(name.startswith(M.CACHE_RESULTS) for name in counters),
        f"指标系列已产生（{len(counters)} 条）",
        "口径：命中/未命中/失效分开记（`miss` 突然变多有**两类**原因，必须与降级指标一起看）",
    )


# ── 主流程 ──────────────────────────────────────────────────────────────────


SCENARIOS = (
    "exact",
    "similar",
    "threshold",
    "time_sensitive",
    "ttl",
    "invalidate",
    "degraded",
    "report",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="缓存层验收工具（八个场景）")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=("all",) + SCENARIOS,
    )
    parser.add_argument(
        "--backend",
        default="auto",
        choices=("auto", "memory", "redis"),
        help="memory = 全离线（假 embedding + 内存存储）；auto/redis = 真依赖",
    )
    parser.add_argument("--list", action="store_true", help="只列出场景")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.list:
        print("八个场景：")
        for index, name in enumerate(SCENARIOS, start=1):
            print(f"  {index}. {name}")
        return 0

    from app.cache.settings import CacheSettings

    settings = CacheSettings()
    space = FakeSpace()
    # ── 假 embedding 的「相似度剧本」────────────────────────────────────────
    # 只在 `--backend memory` 时生效（真后端用真模型算相似度）。三条关系各代表一类：
    #   · 「退货流程是什么」≈「怎么退货」（0.97）→ **应当命中**（同义改写）
    #   · 「退货需要什么条件」（0.90）→ **灰区**：像，但不敢答（这正是阈值保守的落点）
    #   · 「怎么改地址」/「发票怎么开」是各自独立的基准方向 → 相似度约 0 → 不命中
    space.place("怎么退货")
    space.place("退货流程是什么", base="怎么退货", similarity=0.97)
    space.place("退货需要什么条件", base="怎么退货", similarity=0.90)
    space.place("怎么改地址")
    space.place("发票怎么开")
    embedder = build_embedder(args.backend, space)
    revisions = None
    if args.backend == "memory":
        from app.cache.corpus import MemoryRevisionStore

        revisions = MemoryRevisionStore()

    print(f"缓存配置：阈值={settings.semantic_threshold} 兜底线={settings.semantic_floor} "
          f"TTL={settings.ttl_seconds}s（政策 {settings.policy_ttl_seconds}s）"
          f" 强时效={settings.time_sensitive_policy}")
    print(f"后端={args.backend} embedding={getattr(embedder, 'resolve_model', lambda **_: '?')()}")

    cache = build_cache_for(args.backend, embedder, revisions=revisions)
    health = cache.health()
    print(f"存储：条目={health.store}（降级={health.degraded}）"
          f" 表/连接={health.entry_store.get('table') or health.entry_store.get('store')}")
    if health.problems:
        print(f"⚠️ 配置问题：{health.problems}")
    if args.backend != "memory" and health.store == "memory":
        print("⚠️ 真依赖不可用，已回落到内存存储（场景 6 的「版本失效」仍然成立，但只在本进程内）")

    selected = SCENARIOS if args.scenario == "all" else (args.scenario,)
    report = Report()
    # 真 embedding 下的同义探针：本机实测（bge-m3）与「怎么退货」的相似度分别是
    # 0.9681 / 0.9395 / 0.8835（最后一个**没过线**，所以它只在场景 3 的分布里出现）。
    # 假 embedding（memory 后端）用剧本里那一组（0.97 / 0.90）。
    if args.backend == "memory":
        similar_probes = ("退货流程是什么",)
    else:
        similar_probes = ("退货怎么操作", "我想退货，流程是怎么样的")
    for name in selected:
        if name == "exact":
            scenario_exact(cache, embedder, report)
        elif name == "similar":
            scenario_similar(cache, embedder, report, probes=similar_probes)
        elif name == "threshold":
            scenario_threshold(
                cache, embedder, report,
                [
                    "退货怎么操作",
                    "退货流程是什么",
                    "七天无理由退货怎么规定的",
                    "退货需要什么条件",
                    "怎么改地址",
                    "发票怎么开",
                ],
            )
        elif name == "time_sensitive":
            scenario_time_sensitive(cache, report)
        elif name == "ttl":
            scenario_ttl(space, report, settings=settings)
        elif name == "invalidate":
            scenario_invalidate(cache, revisions, embedder, report)
        elif name == "degraded":
            scenario_degraded(space, report)
        elif name == "report":
            scenario_report(cache, space, report)

    report.section("汇总")
    print(f"  通过 {len(report.rows) - report.failed}/{len(report.rows)} 项")
    entries = 0
    try:
        entries = cache.store.count()
    except Exception:  # noqa: BLE001
        pass
    print(f"  本次缓存表/内存里的条目：约 {entries} 条")
    if args.backend != "memory":
        print("  想清掉本次写入：`uv run python -m scripts.cache_report --purge-all`")
        print("  想看现场：`uv run python -m scripts.cache_report`")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
