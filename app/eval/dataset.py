"""golden dataset 的**数据契约与加载口径**（`14.eval.md` §4 第 1 条）。

## 为什么评测数据是一份进 Git 的 JSON（而不是散在测试里的常量）

`14.eval.md` §5 第三条：「评测数据要版本化：golden set 进 Git，问题/答案变更走 review，
否则评测基线会漂移」。这条要求落成三个具体的决定：

1. **数据与代码分家**：`eval/datasets/*.json` 是数据，`app/eval/*.py` 是算分。
   混在一起时，「改了一条标准答案」在 diff 里看起来像「改了算法」，
   review 的注意力会放错地方 —— 而这两类改动的审法完全不同。
2. **每条用例带 `key_facts`（必须命中的事实点）**：`14.eval.md` §5 第二条解释了理由 ——
   纯 LLM 打分不稳定，标注「必须命中的事实」之后可以用**规则 + LLM 双重判分**，
   规则那一路完全离线、完全可复现（`app/eval/metrics.py`）。
3. **`version` 字段必填**：分数只有和「哪一版数据集」绑在一起才有意义。
   同一个 0.9 在 v1 与 v2 上是两个不同的结论（题变难了？变简单了？），
   而报告里只写 0.9 的时候没人分得清。

## 三条加载期的硬校验（宁可加载就炸，也不要算出一个错的分数）

`load_dataset` 会拒绝：缺字段、`expected_sources` 为空（除非显式声明 `expect_empty=True`）、
`key_facts` 与 `reference` **完全无关**（说明标注抄错了）。
最后一条是**本模块最值钱的一条校验**：它拦的是「标准答案与事实点来自不同用例」
这种复制粘贴事故 —— 那种错误不会让任何测试变红，只会让 Faithfulness 永久偏低，
而人会先去怀疑检索。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from app.eval.text import normalize_text

#: 数据集目录（相对仓库根）。`14.eval.md` §3 点名的位置。
DATASET_DIR: Final[str] = "eval/datasets"

#: 两份数据集的**逻辑名** → 文件名。用逻辑名而不是路径，是为了让 CLI 与报告里
#: 出现的名字短且稳定（`--suite rag` 而不是 `--dataset eval/datasets/rag_golden_v1.json`）。
DATASET_FILES: Final[dict[str, str]] = {
    "rag": "rag_golden_v1.json",
    "agent": "agent_golden_v1.json",
}

#: 允许的 `kind` 取值。`rag` = 只考检索与答案质量；`agent` = 还要考路由与工具选择。
KINDS: Final[tuple[str, ...]] = ("rag", "agent")


class DatasetError(ValueError):
    """数据集不符合契约（缺字段 / 引用为空 / 事实点与标准答案无关）。

    单独一个异常类型：它要在 CLI 里被翻成「去改哪个文件、改什么」，
    与「算分时报错」是两类完全不同的处置。
    """


@dataclass(frozen=True)
class EvalCase:
    """一条评测用例（`14.eval.md` §4 第 1 条点名的四个字段 + 可选的意图/身份）。

    | 字段 | 作用 | 不带会怎样 |
    |---|---|---|
    | `question` | 送进链路的**用户原话** | —— |
    | `reference` | 标准答案（Ragas 的 ground truth） | 只能做规则判分，没法量化「答得全不全」 |
    | `key_facts` | **必须命中**的事实点（子串级） | 判分退回纯 LLM 主观打分，离线不可复现 |
    | `expected_sources` | 该问题**应该**检索到的文档 | Recall 无分母、无法计算 |
    | `expect_empty` | 声明「这条在知识库里确实没有答案」 | —— |
    """

    id: str
    question: str
    reference: str
    key_facts: tuple[str, ...] = ()
    expected_sources: tuple[str, ...] = ()
    #: 这条问题**应该**被判成哪个 route（agent 套件用；对客默认 `rag`）
    expected_route: str = "rag"
    #: 提问者角色与 user_id（身份影响 synthesis 模板与 data 分支的行级过滤）
    role: str = "customer"
    user_id: int = 0
    #: 「知识库里本来就没有答案」的用例：**命中 0 条反而是对的**
    expect_empty: bool = False
    #: 该问题应当被判成**强时效**（不进语义缓存）
    time_sensitive: bool = False
    #: 自由备注（报告里原样展示，用来解释这条为什么这么标）
    note: str = ""
    tags: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return "agent" if self.expected_route not in ("rag", "") or self.role != "customer" else "rag"


@dataclass(frozen=True)
class EvalSet:
    """一份 golden dataset（元信息 + 用例）。"""

    name: str
    version: str
    kind: str
    description: str
    cases: tuple[EvalCase, ...]
    source_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self):
        return iter(self.cases)

    def by_id(self, case_id: str) -> EvalCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(f"数据集 {self.name!r} 里没有用例 {case_id!r}")

    def subset(self, ids: list[str] | None = None, limit: int | None = None) -> "EvalSet":
        """按 id 挑选 / 截断（`--only` 与 `--limit` 的落点）。

        截断**只在长度上生效**，不改变任何一条的内容 —— 评测的「跑一半」
        必须是「同样的题只跑了前 N 条」，否则前后两次的数字不可比。
        """
        cases = list(self.cases)
        if ids:
            wanted = [str(item) for item in ids]
            cases = [case for case in cases if case.id in wanted]
            missing = [item for item in wanted if all(case.id != item for case in cases)]
            if missing:
                raise DatasetError(f"数据集 {self.name!r} 里没有这些用例 id：{missing}")
        if limit is not None:
            cases = cases[: max(0, int(limit))]
        return EvalSet(
            name=self.name,
            version=self.version,
            kind=self.kind,
            description=self.description,
            cases=tuple(cases),
            source_path=self.source_path,
            extra=dict(self.extra),
        )

    def summary(self) -> str:
        return f"{self.name} v{self.version}（{self.kind}）{len(self.cases)} 条：{self.description}"


def _require(payload: dict[str, Any], key: str, *, where: str) -> Any:
    if key not in payload:
        raise DatasetError(f"{where} 缺少必填字段 {key!r}")
    return payload[key]


def _as_str_tuple(value: Any, *, where: str, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # 单条字符串是最容易写错的形态（想写列表却写了字符串）：
        # 直接接受它，但**按一整条**处理（而不是按字符拆），并且不报错 ——
        # 这条宽容是刻意的：它让「只有一条事实点」的用例写得短，
        # 而拆字符那种灾难（`"7天"` → `("7","天")`）由这里显式挡住。
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        raise DatasetError(f"{where} 的 {key!r} 必须是字符串或字符串列表，收到 {type(value).__name__}")
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return tuple(out)


def parse_case(payload: Any, *, where: str) -> EvalCase:
    """把一条 JSON 记录解析成 `EvalCase`（并做上面那三条硬校验）。"""
    if not isinstance(payload, dict):
        raise DatasetError(f"{where} 必须是 JSON 对象，收到 {type(payload).__name__}")

    case_id = str(_require(payload, "id", where=where)).strip()
    question = str(_require(payload, "question", where=where)).strip()
    reference = str(_require(payload, "reference", where=where)).strip()
    if not case_id or not question:
        raise DatasetError(f"{where} 的 id / question 不能为空")

    key_facts = _as_str_tuple(payload.get("key_facts"), where=where, key="key_facts")
    expected_sources = _as_str_tuple(
        payload.get("expected_sources") or payload.get("expected_source"),
        where=where,
        key="expected_sources",
    )
    expect_empty = bool(payload.get("expect_empty", False))
    if not expected_sources and not expect_empty:
        raise DatasetError(
            f"{where}（{case_id}）既没有 expected_sources 也没有 expect_empty=True。"
            "「应该检索到哪份文档」是 Recall 的分母，缺了它这条用例只能出答案分、"
            "出不了检索分；若这条问题在知识库里确实没有答案，请显式写 expect_empty=true。"
        )

    role = str(payload.get("role") or "customer").strip() or "customer"
    user_id = int(payload.get("user_id") or 0)
    tags = _as_str_tuple(payload.get("tags"), where=where, key="tags")
    case = EvalCase(
        id=case_id,
        question=question,
        reference=reference,
        key_facts=key_facts,
        expected_sources=expected_sources,
        expected_route=str(payload.get("expected_route") or "rag").strip() or "rag",
        role=role,
        user_id=user_id,
        expect_empty=expect_empty,
        time_sensitive=bool(payload.get("time_sensitive", False)),
        note=str(payload.get("note") or "").strip(),
        tags=tags,
    )
    _assert_facts_related(case, where=where)
    return case


def _assert_facts_related(case: EvalCase, *, where: str) -> None:
    """`key_facts` 的每一条都必须能在 `reference` 里找到（允许数字写法差异）。

    这条校验拦的是「标准答案与事实点来自不同用例」的复制粘贴事故：
    它不会让任何测试变红，只会让 Faithfulness 永久偏低，而人会先去怀疑检索
    （见模块 docstring）。判据用 `normalize_text`（全角/数字/大小写归一），
    所以 `7 天` 与 `7天`、`7` 与 `七` 的写法差异不会被误判。
    """
    if not case.key_facts:
        return
    haystack = normalize_text(case.reference)
    orphans = [fact for fact in case.key_facts if normalize_text(fact) not in haystack]
    if orphans:
        raise DatasetError(
            f"{where}（{case.id}）的 key_facts 里有 {len(orphans)} 条不在 reference 里：{orphans}。"
            "事实点必须逐字（归一后）出现在标准答案里 —— 否则那条用例的满分是拿不到的，"
            "而症状是「Faithfulness 偏低」，看起来像检索的锅（见 dataset.py docstring）。"
        )


def parse_dataset(payload: Any, *, source_path: str = "", name: str = "") -> EvalSet:
    """把一份 JSON 解析成 `EvalSet`（元信息 + 用例，含 id 唯一性校验）。"""
    if not isinstance(payload, dict):
        raise DatasetError(f"数据集顶层必须是 JSON 对象，收到 {type(payload).__name__}")

    where = source_path or name or "dataset"
    set_name = str(payload.get("name") or name or "").strip()
    if not set_name:
        raise DatasetError(f"{where} 缺少 name（数据集名字要进报告，缺了它两份报告无法区分）")
    version = str(payload.get("version") or "").strip()
    if not version:
        raise DatasetError(
            f"{where} 缺少 version：分数只有和「哪一版数据集」绑在一起才有意义"
            "（同一个 0.9 在 v1 与 v2 上是两个不同的结论）"
        )
    kind = str(payload.get("kind") or "rag").strip()
    if kind not in KINDS:
        raise DatasetError(f"{where} 的 kind 必须是 {KINDS} 之一，收到 {kind!r}")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetError(f"{where} 的 cases 必须是非空数组")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_cases, start=1):
        case = parse_case(item, where=f"{where} 第 {index} 条")
        if case.id in seen:
            # 重复 id 会让「按 id 挑用例」与报告里的对照表静默错位（后一条覆盖前一条），
            # 而报告仍然打得出来 —— 这正是要拦的形态。
            raise DatasetError(f"{where} 里 id 重复：{case.id!r}（id 必须唯一）")
        seen.add(case.id)
        cases.append(case)

    return EvalSet(
        name=set_name,
        version=version,
        kind=kind,
        description=str(payload.get("description") or "").strip(),
        cases=tuple(cases),
        source_path=source_path,
        extra={
            key: value
            for key, value in payload.items()
            if key not in {"name", "version", "kind", "description", "cases"}
        },
    )


def dataset_path(name_or_path: str, *, root: str | Path = ".") -> Path:
    """把「逻辑名」或「路径」解析成真实文件路径。

    两种写法都支持（`--suite rag` 与 `--suite eval/datasets/rag_golden_v1.json`），
    因为**临时加一份对照数据集**是评测里最常见的动作（改了什么之后想只跑那几条），
    强制先登记进 `DATASET_FILES` 会让这件事变麻烦，而麻烦的结果是没人做对照。
    """
    root_path = Path(root)
    key = str(name_or_path).strip()
    if key in DATASET_FILES:
        return root_path / DATASET_DIR / DATASET_FILES[key]
    candidate = Path(key)
    return candidate if candidate.is_absolute() else root_path / candidate


def load_dataset(name_or_path: str, *, root: str | Path = ".") -> EvalSet:
    """读一份 golden dataset（`14.eval.md` §2 的 `--suite rag|agent` 的落点）。"""
    path = dataset_path(name_or_path, root=root)
    if not path.exists():
        raise DatasetError(
            f"数据集文件不存在：{path}。已知逻辑名：{sorted(DATASET_FILES)}；"
            "也可以直接给路径（例：--suite eval/datasets/rag_golden_v1.json）。"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DatasetError(f"数据集不是合法 JSON：{path}（{exc}）") from exc
    return parse_dataset(payload, source_path=str(path))


__all__ = [
    "DATASET_DIR",
    "DATASET_FILES",
    "KINDS",
    "DatasetError",
    "EvalCase",
    "EvalSet",
    "dataset_path",
    "load_dataset",
    "parse_case",
    "parse_dataset",
]
