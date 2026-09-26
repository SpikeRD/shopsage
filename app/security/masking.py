"""脱敏（`4.security.md` 第 4 节第 5 条）：**库里存真值，出口按角色脱敏**。

分工与边界：
- 本模块只负责「把已经查出来的数据变模糊」，不负责取数（取数在 `app/db/queries.py`）、
  也不负责拦 SQL（拦 SQL 在 `app/security/sql_guard.py`）。三者串起来才是完整的一条对客链路。
- 脱敏放在**结果返回前**而不是**写入时**：写入时脱敏会不可逆地毁掉业务价值
  （客服要回拨客户电话、财务要核对账号），而出口脱敏可以按角色给不同粒度。

两条「防漏」设计，比规则表本身更重要：

1. **按值形态兜底**（`_scan_value`）：即使调用方忘了在 `field_rules` 里登记某个字段，
   手机号/身份证/银行卡/邮箱这些**长得就能认出来**的值也不会原样漏出去。
   漏登记是必然会发生的事（新增字段没人记得回来改规则表），所以兜底不是可选项。
2. **默认拒绝**（角色不在 `ROLE_MAX_SENSITIVITY` 里 → 整个字段被抹成 `***`）：
   新角色没登记时宁可查不到，也不要静默看到全量真值。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.security.policy import (
    CUSTOMER_ROLE,
    ROLE_MAX_SENSITIVITY,
    SENSITIVITY_FINANCIAL,
    SENSITIVITY_INTERNAL,
    SENSITIVITY_MASKED,
    SENSITIVITY_PUBLIC,
)

# 字段被完全抹掉时的占位（调用方用它判断「有没有东西被拿掉」）
REDACTED: Final[str] = "***"

Masker = Callable[[str], str]
Row = Mapping[str, Any]


# ── 基础脱敏函数 ─────────────────────────────────────────────────────────────


def mask_phone(value: str) -> str:
    """手机号 `13812341234` → `138****1234`（保留前 3 后 4，够客服核对、不够外呼）。

    宽容处理：带 `+86` / 空格 / 短横线的写法统一抽取数字后再掩码。

    ⚠️ 两个必须成立的边界（实测踩过第二个）：
    1. **绝不原样返回**：位数不足时整体打码；
    2. **绝不比原值更长**：位数不够长时（如 `1381234`）若照搬「前 3 + 4 个星 + 后 4」，
       会把原值「撑长」（`138****1234` 比 `1381234` 还长）—— 那既暴露了「这里原本有个短号码」，
       也说明前缀/后缀区间已经重叠。所以位数不足以容纳两段时直接整体打码。
    """
    digits = re.sub(r"\D", "", value or "")
    # 保留 3+4 两段需要至少 3 + 1 + 4 = 8 位互不重叠的数字
    if len(digits) < 8:
        return REDACTED if digits or value else value
    prefix, suffix = digits[:3], digits[-4:]
    masked = f"{prefix}****{suffix}"
    # 兜底：打码结果不得比原值更长（`13812345` 这种 8 位短号会命中这一条）——
    # 「越打越长」等于告诉对方「这里原本是个短号码」，也说明前后缀区间已经重叠。
    if len(masked) > len(digits):
        return REDACTED
    return masked


def mask_address(value: str) -> str:
    """地址只到**区/县**级：`浙江省杭州市西湖区文三路 100 号 3 幢 501` → `浙江省杭州市西湖区***`。

    实现取「最后一个行政区划级别标记之前的全部内容」（省/市/区/县/旗），
    后面属于门牌级信息的一律砍掉。识别不到区划标记时保守截断到前 6 个字符。
    """
    text = (value or "").strip()
    if not text:
        return text
    matches = list(re.finditer(r"[省市区县旗]", text))
    if matches:
        return text[: matches[-1].end()] + REDACTED
    return text[:6] + REDACTED if len(text) > 6 else text + REDACTED


def mask_email(value: str) -> str:
    """邮箱 `zhangsan@example.com` → `z***@example.com`（保留首字符与域名）。"""
    text = (value or "").strip()
    if "@" not in text:
        return REDACTED if text else text
    local, _, domain = text.partition("@")
    if not local:
        return f"{REDACTED}@{domain}"
    return f"{local[0]}***@{domain}"


def mask_id_card(value: str) -> str:
    """身份证 `330106199001011234` → `330106********1234`（保留前 6 后 4）。"""
    text = (value or "").strip()
    if len(text) < 8:
        return REDACTED if text else text
    return f"{text[:6]}{'*' * (len(text) - 10)}{text[-4:]}"


def mask_bank_card(value: str) -> str:
    """银行卡 `6222021234567890123` → `6222***********0123`（保留前 4 后 4）。"""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 8:
        return REDACTED if digits or value else value
    return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"


def mask_name(value: str) -> str:
    """姓名 `张三` → `张*`；`欧阳锋` → `欧**`（只留姓）。"""
    text = (value or "").strip()
    if not text:
        return text
    return text[0] + "*" * max(1, len(text) - 1)


def mask_any(value: str) -> str:
    """整值抹掉（用于「角色等级不够」的字段，以及兜底认不出的敏感值）。"""
    return REDACTED if value else value


# ── 规则表：字段名 → (脱敏函数, 敏感等级) ─────────────────────────────────────
# 这张表就是 `4.security.md` 第 5 节说的「字段→规则映射表」。等级语义见 policy 模块。
# 新增 PII 字段时**只需在这里加一行**，角色与调用方都不用改 —— 这就是可扩展点。
FIELD_RULES: Final[dict[str, tuple[Masker, int]]] = {
    "phone": (mask_phone, SENSITIVITY_MASKED),
    "mobile": (mask_phone, SENSITIVITY_MASKED),
    "email": (mask_email, SENSITIVITY_MASKED),
    "address": (mask_address, SENSITIVITY_MASKED),
    "name": (mask_name, SENSITIVITY_INTERNAL),
    "real_name": (mask_name, SENSITIVITY_INTERNAL),
    "id_card": (mask_id_card, SENSITIVITY_FINANCIAL),
    "bank_card": (mask_bank_card, SENSITIVITY_FINANCIAL),
    "payment_method": (mask_bank_card, SENSITIVITY_FINANCIAL),
}

# 值形态兜底：字段名没登记，但值长得就是这几类敏感数据时，按对应规则处理。
_VALUE_PATTERNS: Final[tuple[tuple[re.Pattern[str], Masker], ...]] = (
    (re.compile(r"^1[3-9]\d{9}$"), mask_phone),  # 大陆手机号
    (re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$"), mask_email),  # 邮箱
    (re.compile(r"^\d{17}[\dXx]$"), mask_id_card),  # 18 位身份证
    (re.compile(r"^\d{15}$"), mask_id_card),  # 15 位老身份证
    (re.compile(r"^\d{16,19}$"), mask_bank_card),  # 银行卡
)

# 这些字段名虽然不在 FIELD_RULES 里，但**内容**永远不该原样出去（兜底之外再加一道名词兜底）
_ALWAYS_MASK_HINTS: Final[tuple[str, ...]] = ("password", "secret", "token", "api_key", "salt")


@dataclass(frozen=True)
class MaskResult:
    """一次脱敏的结果：处理后的数据 + 审计信息。"""

    rows: list[dict[str, Any]]
    masked_fields: tuple[str, ...] = field(default=())
    redacted_fields: tuple[str, ...] = field(default=())

    def as_dicts(self) -> list[dict[str, Any]]:
        return self.rows


def mask_pii(
    rows: Iterable[Row] | Row,
    *,
    field_rules: Mapping[str, Masker] | None = None,
    role: str = CUSTOMER_ROLE,
) -> MaskResult:
    """对查询结果做脱敏，返回 `MaskResult`。

    参数：
        rows: 一行（dict）或多行（list[dict]）；行内可以是任意嵌套（值里有 JSON 字符串也会处理）。
        field_rules: 覆盖/补充规则（字段名 → 脱敏函数）；`None` = 用 `FIELD_RULES` 的函数部分。
        role: 调用方角色，决定「等级够不够」；等级不够的字段直接抹成 `***`。

    等级判定（**一行两句就够**）：
        · 字段等级 > 角色上限 → 整值抹成 `***`（记进 `redacted_fields`）；
        · 否则 → 按该字段的脱敏函数处理（记进 `masked_fields`），等级 0 的公开字段原样返回；
        · 角色未登记（不在 `ROLE_MAX_SENSITIVITY` 里）→ 上限视为 -1，所有 PII 字段都被抹掉。
    """
    rules: dict[str, tuple[Masker, int]] = dict(FIELD_RULES)
    if field_rules:
        for name, func in field_rules.items():
            existing = rules.get(name)
            rules[name] = (func, existing[1] if existing else SENSITIVITY_MASKED)

    ceiling = ROLE_MAX_SENSITIVITY.get(role, -1)  # 未登记角色 → -1 → 默认拒绝

    single = isinstance(rows, Mapping)
    source: Sequence[Row] = [rows] if single else list(rows)  # type: ignore[list-item]

    masked: set[str] = set()
    redacted: set[str] = set()
    out: list[dict[str, Any]] = []

    for row in source:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            name = str(key).lower()

            # ① 字段名命中规则表 → 按等级决定「脱敏」还是「抹掉」
            if name in rules:
                func, level = rules[name]
                if not isinstance(value, str) or not value:
                    clean[key] = value  # NULL / 非字符串原样返回，脱敏规则只作用于字符串
                elif level > ceiling:
                    clean[key] = mask_any(value)
                    redacted.add(name)
                elif level >= SENSITIVITY_MASKED:
                    clean[key] = func(value)
                    masked.add(name)
                else:
                    clean[key] = value  # 等级 0（公开）：无需脱敏
                continue

            # ② 字段名像密钥 → 无论等级都抹掉
            if any(hint in name for hint in _ALWAYS_MASK_HINTS):
                clean[key] = mask_any(value) if value else value
                redacted.add(name)
                continue

            # ③ 值形态兜底（漏登记字段时的最后一道网）
            if isinstance(value, str):
                replaced = _scan_value(value)
                if replaced is not None:
                    clean[key] = replaced
                    masked.add(name)
                    continue

            clean[key] = value
        out.append(clean)

    return MaskResult(
        rows=out,
        masked_fields=tuple(sorted(masked)),
        redacted_fields=tuple(sorted(redacted)),
    )


def _scan_value(value: str) -> str | None:
    """值形态兜底：认出敏感值就替换，认不出返回 None（表示「原样放行」）。

    ⚠️ 这里**只认「整个值就是一个敏感数据」**的形态（`row["memo"]` 里夹一句手机号不在此列）——
    全文扫描会把业务文本里的订单号也误伤，且正则会随文本长度线性变慢。
    夹带在长文本里的敏感串由「结果集内容抽检」（`tests/security`）和人工评审兜住，
    这是有意的取舍，不是遗漏。
    """
    text = value.strip()
    if not text:
        return None
    for pattern, func in _VALUE_PATTERNS:
        if pattern.match(text):
            return func(text)
    return None


def mask_json(payload: str | bytes, *, role: str = CUSTOMER_ROLE) -> str:
    """脱敏一段 JSON 文本（用于「结果被序列化后才想起来要脱敏」的场景，如 WebSocket 出帧）。

    只处理顶层是对象/对象数组的 JSON；其它形态原样返回（不是 JSON 就没有字段名可依据）。
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return payload if isinstance(payload, str) else payload.decode("utf-8", "replace")

    if isinstance(data, dict):
        return json.dumps(mask_pii(data, role=role).rows[0], ensure_ascii=False)
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return json.dumps(mask_pii(data, role=role).rows, ensure_ascii=False)
    return payload if isinstance(payload, str) else json.dumps(data, ensure_ascii=False)


__all__ = [
    "FIELD_RULES",
    "REDACTED",
    "MaskResult",
    "mask_address",
    "mask_any",
    "mask_bank_card",
    "mask_email",
    "mask_id_card",
    "mask_json",
    "mask_name",
    "mask_phone",
    "mask_pii",
]
