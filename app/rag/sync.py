"""增量同步：监听文档变更 → 只重建受影响的片段（F1「更新 < 1min 可见」的落点）。

两种触发方式，都落在同一个 `RagSyncWorker.sync_once()` 上：

| 方式 | 适用 | 说明 |
|---|---|---|
| **轮询**（`--interval`） | 开发期、小知识库、无 CDC 的环境 | 比对「文件内容指纹」与库里存的指纹，变了才入库；没变的那次**零成本**（一次 SELECT + 一次目录遍历） |
| **一次性**（`--once`） | CI / 上线前的灌库步骤 / 定时任务（cron、K8s CronJob） | 与 `scripts/load_knowledge_base.py` 同一件事，只是多了「按需清理已删除的文件」 |

**为什么不做文件系统事件（watchdog/inotify）**：事件在容器/网络盘/编辑器「原子写」
（临时文件 + rename）下的行为差异很大，容易漏事件；而漏事件的症状是「检索到旧内容」——
正是 F1 要解决的问题。轮询的代价是「一次目录遍历 + 一次 SELECT」，
在知识库这个量级（几十~几千份文档）完全可接受，且**行为确定**。

**已删除的文件怎么办**：默认**不动**（保守）。原因是一份文件从磁盘上消失可能是
「误删」「还没同步过来的挂载」「临时移走」，直接下线文档会让一次误操作变成一次检索事故。
要清理请显式 `--prune`（或调 `sync_once(prune=True)`）—— 让「删除」成为一个**明确的动作**。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.rag.ingest import DirectoryIngestReport, Embedder, delete_document, ingest_document
from app.rag.parse import SUPPORTED_SUFFIXES, parse_text
from app.rag.settings import RagSettings
from app.rag.store import RagStore

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    """一次同步的结果。"""

    ingested: list[Any] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    scanned: int = 0
    skipped: int = 0
    elapsed_ms: float = 0.0

    @property
    def changed(self) -> int:
        return sum(1 for item in self.ingested if item.action in ("created", "updated"))

    @property
    def embedded(self) -> int:
        return sum(getattr(item, "embedded", 0) for item in self.ingested)

    @property
    def chunks(self) -> int:
        return sum(getattr(item, "chunks_total", 0) for item in self.ingested)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "skipped": self.skipped,
            "changed": self.changed,
            "embedded": self.embedded,
            "chunks": self.chunks,
            "pruned": list(self.pruned),
            "missing": list(self.missing),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "documents": [
                {
                    "source": item.source,
                    "action": item.action,
                    "embedded": getattr(item, "embedded", 0),
                }
                for item in self.ingested
            ],
        }


def scan_documents(
    root: str | Path,
    *,
    suffixes: Sequence[str] = SUPPORTED_SUFFIXES,
    recursive: bool = True,
) -> list[Path]:
    """列出知识库目录下**支持的**文件（按路径排序：入库顺序稳定，日志才好比对）。"""
    path = Path(root)
    if not path.exists():
        raise FileNotFoundError(f"知识库目录不存在：{path}")
    pattern = "**/*" if recursive else "*"
    return sorted(
        item
        for item in path.glob(pattern)
        if item.is_file() and item.suffix.lower() in suffixes
    )


class RagSyncWorker:
    """增量同步器：`sync_once()` 是全部逻辑，`run_forever()` 只是加了个循环与睡眠。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        store: RagStore | None = None,
        settings: RagSettings | None = None,
        embedder: Embedder | None = None,
        category: str = "general",
    ) -> None:
        self.settings = settings or RagSettings.from_settings()
        self.root = Path(root or self.settings.knowledge_dir)
        self.store = store or RagStore(dimension=self.settings.vector_dim)
        self.embedder = embedder
        self.category = category
        self._stopped = False

    async def sync_once(
        self,
        *,
        prune: bool = False,
        force: bool = False,
        recursive: bool = True,
    ) -> SyncReport:
        """扫一遍目录，把「指纹变了的文档」增量入库。

        参数：
            prune: 磁盘上已消失的文档是否下线（默认否，理由见模块 docstring）。
            force: 忽略文档指纹，全部走一遍（片段级增量仍然生效）。
        """
        import time

        started = time.perf_counter()
        files = scan_documents(self.root, recursive=recursive)
        report = SyncReport(scanned=len(files))

        for file in files:
            # 先用「文件内容指纹」做一次廉价短路：与库里存的对比，一致就整篇跳过
            # （连解析都不做 —— 解析 PDF/Markdown 是同步里最贵的一步，仅次于 embedding）。
            try:
                parsed = await asyncio.to_thread(parse_text, file)
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不该中断整轮同步
                logger.warning("同步：解析失败，已跳过 %s（%s: %s）", file, type(exc).__name__, exc)
                report.skipped += 1
                continue

            from app.rag.ingest import document_hash

            state = await asyncio.to_thread(self.store.document_state, parsed.source)
            if (
                not force
                and state is not None
                and state.get("is_active")
                and state.get("doc_hash") == document_hash(parsed.text)
            ):
                report.skipped += 1
                continue

            result = await ingest_document(
                file,
                content=parsed.text,
                category=self.category,
                store=self.store,
                settings=self.settings,
                embedder=self.embedder,
                force=force,
            )
            report.ingested.append(result)

        if prune:
            on_disk = {file.as_posix() for file in files}
            # 「盘上没有、库里还活着」的文档才下线。注意用 as_posix() 归一化：
            # Windows 的 `\` 与库里的 `/` 不一致会让**每一份**文档都被判为「已删除」。
            known = await asyncio.to_thread(self._list_active_sources)
            for source in known:
                if source in on_disk:
                    continue
                if not Path(source).exists():
                    report.missing.append(source)
                    await delete_document(source, store=self.store, settings=self.settings)
                    report.pruned.append(source)

        report.elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "同步完成：扫描 %d，跳过 %d，变更 %d，重嵌 %d 个片段，下线 %d，耗时 %.0fms",
            report.scanned,
            report.skipped,
            report.changed,
            report.embedded,
            len(report.pruned),
            report.elapsed_ms,
        )
        return report

    def _list_active_sources(self) -> list[str]:
        """库里所有活跃文档的 `source`（prune 用）。"""
        from sqlalchemy import text

        with self.store.read_engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"select source from {self.store.documents_table} where is_active order by source"
                )
            ).all()
        return [str(row.source) for row in rows]

    async def run_forever(self, *, interval: float = 30.0, prune: bool = False) -> None:
        """轮询循环（Ctrl+C / `stop()` 退出）。

        异常**不**让它退出：一轮同步失败（数据库抖动、某个文件坏了）不该让同步器倒下，
        否则「同步器挂了」这件事要等到检索到旧内容时才会被发现。
        """
        logger.info("增量同步开始轮询：root=%s interval=%.0fs prune=%s", self.root, interval, prune)
        while not self._stopped:
            try:
                await self.sync_once(prune=prune)
            except Exception:  # noqa: BLE001 - 见 docstring
                logger.exception("同步这一轮失败（下轮继续）")
            await asyncio.sleep(interval)

    def stop(self) -> None:
        """请求退出轮询（下一个睡眠点生效）。"""
        self._stopped = True


__all__ = ["RagSyncWorker", "SyncReport", "scan_documents"]
