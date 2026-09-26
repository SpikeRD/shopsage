"""脱敏用例（`4.security.md` §7「脱敏」）。

两条判据：
1. **规则本身正确**：手机号 `138****1234`、地址只到区级、身份证/银行卡保留首尾；
2. **按角色分级**：等级不够的字段**整值抹掉**（`***`），等级够的按规则脱敏 ——
   并且新增/未登记角色必须落到「默认拒绝」那一侧。

另外钉住两条「防漏」设计：字段漏登记时的**值形态兜底**、以及**序列化出口**（`mask_json`）。
"""

from __future__ import annotations

import pytest

from app.security.masking import (
    FIELD_RULES,
    REDACTED,
    mask_address,
    mask_bank_card,
    mask_email,
    mask_id_card,
    mask_json,
    mask_name,
    mask_phone,
    mask_pii,
)
from app.security.policy import CUSTOMER_ROLE


# ── ① 规则本身 ───────────────────────────────────────────────────────────────


def test_mask_phone_matches_spec():
    """`4.security.md` §7 原文用例：`13812341234` → `138****1234`。"""
    assert mask_phone("13812341234") == "138****1234"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("13812341234", "138****1234"),
        ("+86 138-1234-1234", "861****1234"),  # 带国码与分隔符：抽数字后再掩码
        ("138 1234 1234", "138****1234"),
        ("13812341234 ", "138****1234"),
    ],
)
def test_mask_phone_variants(raw: str, expected: str):
    assert mask_phone(raw) == expected


@pytest.mark.parametrize("bad", ["123", "1381234", "13812345", "138123456", "abcdefg", "--"])
def test_mask_phone_never_returns_raw_or_keeps_digits(bad: str):
    """短值/非号码：**绝不原样返回**，且**一位数字都不保留**。

    （「不能比原值更长」也值得记一笔：`1381234` 这类短号若照搬「前3+4星+后4」会被撑长，
    等于暴露「这里原本是个短号码」—— 实现里已经改成整体打码。）
    """
    result = mask_phone(bad)
    assert result != bad
    assert set(result) <= {"*"}
    assert not any(ch.isdigit() for ch in result)


def test_mask_address_stops_at_district():
    """地址只到区级：门牌/楼栋/门牌号一律砍掉。"""
    assert mask_address("浙江省杭州市西湖区文三路100号3幢501") == f"浙江省杭州市西湖区{REDACTED}"
    assert mask_address("北京市朝阳区建国路1号") == f"北京市朝阳区{REDACTED}"
    assert mask_address("上海市浦东新区世纪大道100号") == f"上海市浦东新区{REDACTED}"
    assert "文三路" not in mask_address("浙江省杭州市西湖区文三路100号")


def test_mask_address_without_district_marker_is_truncated():
    """识别不到区划标记时保守截断，而不是原样返回。"""
    result = mask_address("文三路100号3幢501")
    assert result.endswith(REDACTED)
    assert "501" not in result


def test_mask_email_and_id_and_bank():
    assert mask_email("zhangsan@example.com") == "z***@example.com"
    assert mask_id_card("330106199001011234") == "330106********1234"
    assert mask_bank_card("6222021234567890123") == "6222***********0123"
    assert mask_name("张三") == "张*"
    assert mask_name("欧阳锋") == "欧**"


def test_every_field_rule_is_callable_and_has_level():
    """规则表结构自检：每个字段都要有函数与等级，且等级非负。"""
    for name, (func, level) in FIELD_RULES.items():
        assert callable(func), name
        assert isinstance(level, int) and level >= 0, name


# ── ② 角色分级 ───────────────────────────────────────────────────────────────

_ROW = {
    "order_id": 7,
    "phone": "13812341234",
    "address": "浙江省杭州市西湖区文三路100号",
    "email": "zhangsan@example.com",
    "name": "张三",
    "id_card": "330106199001011234",
    "status": "paid",
}


def test_customer_sees_masked_pii_but_never_raw():
    """对客路径：自己的数据也给脱敏值（行级过滤保证只看得到自己那行）。"""
    result = mask_pii(_ROW, role=CUSTOMER_ROLE)
    row = result.rows[0]
    assert row["phone"] == "138****1234"
    assert row["address"] == f"浙江省杭州市西湖区{REDACTED}"
    assert row["id_card"] == REDACTED  # 等级 3 > 客户上限 1 → 抹掉
    assert row["status"] == "paid"  # 非 PII 字段不受影响
    assert set(result.masked_fields) >= {"phone", "address", "email"}


def test_employee_levels_are_tiered():
    """低级别员工看不到高级别字段，高级别依次放开（`4.security.md` §7 的员工分级用例）。"""
    floor_pii = mask_pii(_ROW, role="customer_service").rows[0]
    employee = mask_pii(_ROW, role="internal_employee").rows[0]
    manager = mask_pii(_ROW, role="manager").rows[0]
    auditor = mask_pii(_ROW, role="finance_auditor").rows[0]

    # 客诉一线：连脱敏值都看不到
    assert floor_pii["phone"] == REDACTED
    assert floor_pii["name"] == REDACTED

    # 普通员工：看得到脱敏手机号，看不到真实姓名
    assert employee["phone"] == "138****1234"
    assert employee["name"] == REDACTED

    # 主管：看得到（脱敏的）姓名
    assert manager["name"] == "张*"

    # 财务审计：证件号/支付方式也按规则脱敏（但仍不是明文）
    assert auditor["id_card"] == "330106********1234"
    assert auditor["phone"] == "138****1234"


def test_unknown_role_is_denied_by_default():
    """未登记角色：所有 PII 字段被抹掉（默认拒绝，不是默认放行）。"""
    for role in ("", "unknown_role", "intern", "root"):
        row = mask_pii(_ROW, role=role).rows[0]
        for field in ("phone", "address", "email", "name", "id_card"):
            assert row[field] == REDACTED, f"{role} 竟然看到了 {field}"


def test_no_role_can_ever_see_raw_pii():
    """**总闸**：遍历所有登记角色 + 若干乱写角色，任何 PII 字段都不允许原样出现。

    这条是为「将来加了新角色/新字段忘了登记」准备的兜底断言 ——
    等级表一旦被改错（比如把某角色上限提到 3 以上），这里立刻红。
    """
    raw_values = {"13812341234", "浙江省杭州市西湖区文三路100号", "zhangsan@example.com", "330106199001011234"}
    from app.security.policy import ROLE_MAX_SENSITIVITY

    for role in [*ROLE_MAX_SENSITIVITY, "", "hacker", "finance"]:
        row = mask_pii(_ROW, role=role).rows[0]
        for field, value in row.items():
            if field in {"order_id", "status"}:
                continue
            assert value not in raw_values, f"角色 {role} 的字段 {field} 泄露了明文：{value}"


def test_mask_pii_accepts_single_row_and_list():
    single = mask_pii({"phone": "13812341234"}, role="internal_employee")
    assert isinstance(single.rows, list) and len(single.rows) == 1

    many = mask_pii([{"phone": "13812341234"}, {"phone": "13900001111"}], role="internal_employee")
    assert [r["phone"] for r in many.rows] == ["138****1234", "139****1111"]


def test_mask_pii_preserves_none_and_empty():
    """NULL / 空串原样返回：脱敏规则只作用于字符串，别把 NULL 变成 `***`（会污染统计）。"""
    row = mask_pii({"phone": None, "address": "", "email": "a@b.com"}, role="manager").rows[0]
    assert row["phone"] is None
    assert row["address"] == ""
    assert row["email"] == "a***@b.com"


def test_field_rules_can_be_extended_by_caller():
    """字段→规则映射表可扩展：新增字段不必改角色表。"""
    row = mask_pii(
        {"nickname": "老张同学", "phone": "13812341234"},
        field_rules={"nickname": mask_name},
        role="manager",
    ).rows[0]
    assert row["nickname"] == "老***"  # 用 mask_name 处理
    assert row["phone"] == "138****1234"


# ── ③ 防漏：值形态兜底与序列化出口 ──────────────────────────────────────────


def test_value_pattern_fallback_catches_unknown_field_names():
    """字段名漏登记时，值形态兜底仍要拦住（新增字段没人记得回来改规则表是常态）。"""
    row = mask_pii(
        {
            "memo": "13812341234",  # 手机号藏在没登记的字段名里
            "contact": "zhangsan@example.com",
            "card_no": "6222021234567890123",
            "note": "正常业务文本",
        },
        role="manager",
    ).rows[0]
    assert row["memo"] == "138****1234"
    assert row["contact"] == "z***@example.com"
    assert row["card_no"] == "6222***********0123"
    assert row["note"] == "正常业务文本"  # 不误伤普通文本


@pytest.mark.parametrize("name", ["password", "api_key", "access_token", "user_secret"])
def test_secret_like_field_names_are_always_redacted(name: str):
    """字段名像密钥 → 无论角色等级都抹掉。"""
    row = mask_pii({name: "super-secret-value"}, role="finance_auditor").rows[0]
    assert row[name] == REDACTED


def test_mask_json_handles_object_array_and_non_json():
    obj = mask_json('{"phone": "13812341234", "order_id": 7}', role="internal_employee")
    assert "138****1234" in obj and '"order_id": 7' in obj

    arr = mask_json('[{"phone": "13812341234"}]', role="internal_employee")
    assert "138****1234" in arr

    assert mask_json("not json at all", role="internal_employee") == "not json at all"


def test_long_text_with_embedded_phone_is_documented_gap():
    """**已知取舍**：夹在长文本里的手机号不会被兜底拦截（只认「整值即敏感值」）。

    这条测试的作用是把取舍写进代码：它现在**通过**（不脱敏），如果有人把兜底改成
    全文正则，这条会红 —— 那时应当同时评估误伤率（订单号/商品编号也会被吃掉）与性能。
    """
    row = mask_pii({"memo": "客户说手机号 13812341234 打不通"}, role="manager").rows[0]
    assert "13812341234" in row["memo"], "兜底规则变了？请同步更新手册里的已知取舍说明"
