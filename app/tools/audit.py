"""工具调用的审计记录（`6.tools.md` 第 4 节第 2 条：调用者 / 时间 / 参数 / 结果行数）。

为什么审计要单独成模块，而不是每个工具里写一句 `logger.info`：

1. **字段集合必须一致**：审计的用途是「事后能回答谁在什么时候干了什么」，
   五条日志字段各写各的，事后就得写五段解析代码；这里固定成一张表（`AuditRecord`）。
2. **打码规则必须复用安全模块**：参数里可能夹着手机号、退款原因、密钥；
   `app.security.masking` 已经有「字段规则 + 值形态兜底」，审计**绝不另写一套正则**
   （两套规则必然漂移，而漏掉的那一套就是泄露点）。
3. **失败也要留痕**：审计只在成功时记录，等于「越权尝试不留证据」。
   本模块的 `audit_tool_call()` 是上下文管理器，**异常路径同样落一条 WARNING**，
   且是在调用方拿到异常**之前**落 —— D1/D2 的攻击用例就是这么变成证据的。

日志格式：一条 JSON 行（`tool_call {json}` / `tool_error {json}`）。
选 JSON 而不是人类可读串，是因为它同时满足两个消费者：
人用 `Select-String` 直接看，机器（第 11 步的采集、第 19 步的告警）直接 `json.loads`。
`extra={"tool_audit": record.as_dict()}` 同时把记录挂到 LogRecord 上，
测试与 handler 不必再解析字符串（`caplog.records[-1].tool_audit["tool"]`）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Final

from app.security.masking import mask_pii
from app.tools.errors import ToolContractError

logger = logging.getLogger("app.tools.audit")

# 审计日志用的**最严角色**口径：日志会被很多人、很多系统看到，不该出现任何等级的 PII。
# `customer_service` 在 `policy.ROLE_MAX_SENSITIVITY` 里的上限是 SENSITIVITY_PUBLIC(0)，
# 意味着所有等级 > 0 的字段（手机号/地址/姓名/证件/银行卡）整值抹成 `***`。
AUDIT_REDACTION_ROLE: Final[str] = "customer_service"

# 单个参数值在日志里的最大长度：SQL 与查询参数可能很长，审计不该变成「日志放大攻击」的入口
MAX_VALUE_CHARS: Final[int] = 120
_TRUNCATED_MARK: Final[str] = "…(截断)"

# 标记「谁在调用工具」。约定的取值见 docs/runbook/6.tools-runbook.md §3.3：
#   agent:data / agent:analysis / mcp:sql_query / api:chat / test
CALLER_PREFIXES: Final[tuple[str, ...]] = ("agent:", "mcp:", "api:", "script:", "test")


def new_audit_id() -> str:
    """本次工具调用的审计 id（`tc-` 前缀便于 grep）。

    它同时出现在日志行与工具返回的 `meta["audit_id"]` 里 ——
    「用户报的那次查询到底走没走只读角色、行数截断没有」靠这一个 id 就能串起来。
    """
    return f"tc-{uuid.uuid4().hex[:12]}"


def _shrink(value: Any) -> Any:
    """把任意值收敛成「JSON 可序列化 + 长度有界」的形态。

    保留嵌套结构（dict/list）而不是 `repr()` 成一坨：审计的价值一半在于「参数长什么样」，
    但 `Decimal` / `datetime` / 自定义对象必须降级，否则 `json.dumps` 会直接炸掉整条日志。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_VALUE_CHARS:
            return value
        return value[:MAX_VALUE_CHARS] + _TRUNCATED_MARK
    if isinstance(value, Mapping):
        return {str(k): _shrink(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_shrink(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return repr(value)


def redact_args(args: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """参数脱敏 + 长度收敛，返回 `(可入日志的参数, 被打码的字段路径)`。

    复用 `mask_pii(role=AUDIT_REDACTION_ROLE)` 而不是自己写敏感键正则：
    安全模块的规则表会因为新字段而更新（`FIELD_RULES`），审计跟着自动生效。

    ⚠️ **必须递归**（实测踩到的坑）：`mask_pii` 只按**顶层字段名**判规则，
    而 SQL 的绑定参数恰恰都躺在 `params` 这个嵌套 dict 里 ——
    只打顶层时 `{"params": {"phone": "13812341234"}}` 会**原样进日志**。
    工具参数天然是嵌套的（`params` / `rows` / 各种 options），所以每一层都要过一遍规则，
    并把命中路径记下来（`params.phone`），这样「为什么日志里看不到」是可回答的。
    """
    result = mask_pii(dict(args), role=AUDIT_REDACTION_ROLE)
    fields: set[str] = set(result.masked_fields) | set(result.redacted_fields)

    out: dict[str, Any] = {}
    for key, value in result.rows[0].items():
        redacted, nested = _redact_nested(value)
        out[str(key)] = _shrink(redacted)
        fields |= {f"{key}.{field}" for field in nested}
    return out, tuple(sorted(fields))


def _redact_nested(value: Any) -> tuple[Any, tuple[str, ...]]:
    """对嵌套结构逐层套用同一套脱敏规则（dict → 递归；list → 逐项递归）。"""
    if isinstance(value, Mapping):
        return redact_args(value)
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        fields: list[str] = []
        for index, item in enumerate(value):
            redacted, nested = _redact_nested(item)
            items.append(redacted)
            fields.extend(f"[{index}].{field}" for field in nested)
        return items, tuple(fields)
    return value, ()


@dataclass(frozen=True)
class AuditRecord:
    """一条审计记录（`6.tools.md` 第 4 节第 2 条的字段集合）。"""

    audit_id: str
    tool: str
    caller: str
    started_at: str  # ISO-8601（UTC），带毫秒；库里用的是 timestamptz，日志用 UTC 免歧义
    duration_ms: int
    outcome: str  # "ok" | "error"
    rows: int | None  # 结果行数（写操作 = 1，查询 = 返回行数）
    error_type: str | None
    args: dict[str, Any]  # 已脱敏、已截断
    details: dict[str, Any] = field(default_factory=dict)  # 工具自定义字段（truncated / created …）
    redacted_args: tuple[str, ...] = ()  # 哪些参数被打过码（便于回答「日志里为什么看不到」）

    def as_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "tool": self.tool,
            "caller": self.caller,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "rows": self.rows,
            "error_type": self.error_type,
            "args": dict(self.args),
            "details": dict(self.details),
            "redacted_args": list(self.redacted_args),
        }

    def with_details(self, extra: Mapping[str, Any]) -> "AuditRecord":
        """副本 + 追加字段（`details` 是 frozen dataclass 里的可变 dict，改它要显式复制一份）。"""
        return replace(self, details={**self.details, **extra})

    def to_json(self) -> str:
        """一行 JSON（`ensure_ascii=False` 保留中文可读性；`default=str` 兜住漏网的怪类型）。"""
        return json.dumps(self.as_dict(), ensure_ascii=False, default=str)


class AuditHandle:
    """工具内部用它填写「结果行数」与自定义字段。

    用法（见 `app/tools/sql_executor.py`）：

        with audit_tool_call("sql_query", caller=caller, sql=sql) as handle:
            ...
            handle.rows = len(rows)
            handle.details["truncated"] = truncated
    """

    def __init__(self, tool: str, caller: str, args: Mapping[str, Any]) -> None:
        if not isinstance(caller, str) or not caller.strip():
            # 审计不可绕过：没有调用者就没有「谁干的」这个字段，宁可拒绝执行。
            raise ToolContractError(
                "工具调用必须显式传 caller（审计字段：谁在调用工具）。"
                f"约定取值如 {', '.join(CALLER_PREFIXES)}…（见 6.tools-runbook §3.3）"
            )
        self.tool = tool
        self.caller = caller.strip()
        self.audit_id = new_audit_id()
        self.rows: int | None = None
        self.details: dict[str, Any] = {}
        self._t0 = time.perf_counter()
        self._started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._args, self._redacted = redact_args(args)

    def _snapshot(self, outcome: str, error_type: str | None) -> AuditRecord:
        return AuditRecord(
            audit_id=self.audit_id,
            tool=self.tool,
            caller=self.caller,
            started_at=self._started_at,
            duration_ms=int((time.perf_counter() - self._t0) * 1000),
            outcome=outcome,
            rows=self.rows,
            error_type=error_type,
            args=self._args,
            details=dict(self.details),
            redacted_args=self._redacted,
        )

    def record_ok(self) -> AuditRecord:
        return self._snapshot("ok", None)

    def record_error(self, exc: BaseException) -> AuditRecord:
        """失败记录里额外带一条 `error`（截断后的报文），否则「为什么失败」还得去翻别的日志。"""
        return self._snapshot("error", type(exc).__name__).with_details({"error": _shrink(str(exc))})


@contextmanager
def audit_tool_call(tool: str, *, caller: str, **args: Any) -> Iterator[AuditHandle]:
    """给一次工具调用包上审计：成功记 INFO，失败记 WARNING，**两种情况都落一条**。"""
    handle = AuditHandle(tool, caller, args)
    try:
        yield handle
    except BaseException as exc:  # noqa: BLE001 - 审计要覆盖所有失败形态，包括取消
        record = handle.record_error(exc)
        logger.warning(
            "tool_error %s", record.to_json(), extra={"tool_audit": record.as_dict()}
        )
        raise
    record = handle.record_ok()
    logger.info("tool_call %s", record.to_json(), extra={"tool_audit": record.as_dict()})


def audit_records(caplog_records: Sequence[logging.LogRecord]) -> list[dict[str, Any]]:
    """从 `caplog.records` 里取出所有审计记录（测试与排障脚本共用，避免各写一份解析）。

    ⚠️ 只认 `extra={"tool_audit": ...}`，不解析日志文本 —— 文本格式属于人看的输出，
    断言不该绑在它的排版上（改一个字就红一片的测试没有价值）。
    """
    return [r.tool_audit for r in caplog_records if isinstance(getattr(r, "tool_audit", None), dict)]


__all__ = [
    "AUDIT_REDACTION_ROLE",
    "CALLER_PREFIXES",
    "AuditHandle",
    "AuditRecord",
    "audit_records",
    "audit_tool_call",
    "new_audit_id",
    "redact_args",
]
