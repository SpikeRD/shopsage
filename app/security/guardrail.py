"""提示词注入检测（D2 的第一道闸）。

**这道闸不是防线，只是减速带** —— 这一点必须写在最前面，否则会误导读者以为
「检测到了关键词就安全了」。真正兜底的是 `sql_guard`（AST 校验）与只读数据库角色：
即使模型被诱导生成了 `DROP TABLE users`，那条 SQL 也过不了 `validate_sql`；
即使 `validate_sql` 被绕过，只读角色在数据库层还会再拒一次。

所以本模块的定位是「**早失败 + 留证据**」：
- 早失败：明显的注入尝试在进入 LLM 之前就被拒，省掉一次模型调用与一次潜在的数据泄露；
- 留证据：命中的规则名与片段进日志/评测集，用来攒真实攻击样本（第 16 步 eval 的语料）。

关键词检测天然有**绕过**与**误杀**两侧误差，实现上做了两件事压低误杀：
1. 规则要求「攻击意图 + 目标」同时出现（如「忽略/无视…之前的指令」），而不是单个词命中；
2. 命中后抛出的异常带 `matched` 明细，方便人工复核与调参（而不是只给一个 True/False）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final

# 常见混淆手段的归一化：全角转半角、去掉零宽字符、压缩空白/连续标点。
# 不做这一步的话，「ｄｒｏｐ　ｔａｂｌｅ」这种全角写法能轻松绕过关键词匹配。
_ZERO_WIDTH: Final[re.Pattern[str]] = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_WS_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")

# 规则表：规则名 → 正则。规则名要稳定（进日志、进评测集），正则允许迭代。
INJECTION_RULES: Final[dict[str, re.Pattern[str]]] = {
    # 「忽略/无视/忘记 之前的指令」——最经典的越狱开头
    "ignore_instructions": re.compile(
        r"(忽略|无视|忘记|不用管|不要理|跳过)[^。\n]{0,12}(之前|上面|以上|前面|先前|所有)[^。\n]{0,6}(指令|要求|规则|提示|设定|限制)"
        r"|ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|rules?)",
        re.IGNORECASE,
    ),
    # 要求扮演/切换到无限制模式
    "role_override": re.compile(
        r"(你现在是|从现在开始你是|扮演一个?没有限制|开发者模式|越狱模式|DAN模式)"
        r"|(you\s+are\s+now|act\s+as)\s+(a\s+)?(unrestricted|jailbroken|dan)\b",
        re.IGNORECASE,
    ),
    # 直接索要敏感字段/整表
    "exfiltrate_pii": re.compile(
        r"(所有|全部|整个|全量)[^。\n]{0,10}(用户|客户|会员)[^。\n]{0,10}(手机|电话|地址|邮箱|身份证|资料|信息|数据)"
        r"|(把|给我|导出|dump)[^。\n]{0,8}(users?|用户表|员工表)[^。\n]{0,8}(全|所有|都)"
        r"|select\s+\*\s+from\s+(users|employees)\b",
        re.IGNORECASE,
    ),
    # 索要别的用户的数据（越权诱导）
    "cross_user_access": re.compile(
        r"(查|看|显示|列出)[^。\n]{0,8}(别人|其他用户|其他客户|全部用户)[^。\n]{0,8}(的)?[^。\n]{0,6}(订单|退款|工单|信息|资料)"
        r"|user_id\s*=\s*(?!:?\w*current|:?\w*self)\d+\s*[^。\n]{0,6}(所有人|不管|任意)",
        re.IGNORECASE,
    ),
    # 绕开行数/白名单限制
    "bypass_guard": re.compile(
        r"(绕过|关闭|禁用|解除|去掉)[^。\n]{0,8}(限制|校验|检查|白名单|过滤器|安全)"
        r"|(不要|别)(加|带)\s*(limit|where|过滤)"
        r"|limit\s+999999",
        re.IGNORECASE,
    ),
    # 诱导写操作（本系统读路径绝不允许写）
    "write_intent": re.compile(
        r"(删除|清空|改掉|更新|修改)[^。\n]{0,8}(数据库|表|订单|用户|库存)"
        r"|\b(drop|truncate)\s+table\b|\bdelete\s+from\b|\bupdate\s+\w+\s+set\b",
        re.IGNORECASE,
    ),
}


class PromptInjectionError(Exception):
    """输入被判定为提示词注入攻击。"""

    def __init__(self, message: str, *, rules: tuple[str, ...] = (), snippet: str = "") -> None:
        super().__init__(message)
        self.rules = rules
        self.snippet = snippet


@dataclass(frozen=True)
class InjectionScan:
    """扫描结果（**返回而不抛异常**的形态，给需要「记录但仍继续」的场景用）。"""

    text: str
    matched_rules: tuple[str, ...] = field(default=())
    snippets: tuple[str, ...] = field(default=())

    @property
    def is_suspicious(self) -> bool:
        return bool(self.matched_rules)


def normalize(text: str) -> str:
    """归一化：NFKC（全角→半角）+ 零宽字符→空格 + 压缩空白。

    实测这一步是必要的，而且**零宽字符必须换成空格而不是直接删掉**：
    `ｄｒｏｐ　ｔａｂｌｅ ｕｓｅｒｓ` 不归一化匹配不到任何规则；而
    `drop\\u200btable users` 若把零宽字符直接删掉会变成 `droptable users`
    —— 一个字，反把 `\\bdrop\\s+table\\b` 这种判据给**拆了**（等于用混淆字符串绕过了检测）。
    换成空格再压缩，两种写法都归一成 `drop table users`。
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _ZERO_WIDTH.sub(" ", normalized)
    return _WS_RUN.sub(" ", normalized).strip()


def scan(text: str) -> InjectionScan:
    """扫描文本，返回命中的规则名与片段（不发异常）。"""
    normalized = normalize(text)
    rules: list[str] = []
    snippets: list[str] = []
    for name, pattern in INJECTION_RULES.items():
        match = pattern.search(normalized)
        if match:
            rules.append(name)
            snippets.append(match.group(0)[:80])
    return InjectionScan(text=normalized, matched_rules=tuple(rules), snippets=tuple(snippets))


def assert_no_injection(text: str, *, max_length: int = 4000) -> InjectionScan:
    """判定输入是否含注入意图；命中就抛 `PromptInjectionError`。

    长度上限也在这里卡住：超长输入既可能是 DoS（把上下文塞满），
    也可能把攻击串藏在几千字之后 —— 宁可要求调用方先截断/分段。
    """
    if text is None:
        raise PromptInjectionError("输入为空")

    normalized = normalize(text)
    if len(normalized) > max_length:
        raise PromptInjectionError(
            f"输入超长（{len(normalized)} > {max_length} 字符）：请截断或分段后再提交",
            rules=("too_long",),
        )

    result = scan(normalized)
    if result.is_suspicious:
        raise PromptInjectionError(
            f"疑似提示词注入，已拒绝：命中规则 {', '.join(result.matched_rules)}",
            rules=result.matched_rules,
            snippet=result.snippets[0] if result.snippets else "",
        )
    return result


def assert_safe_llm_output(sql: str) -> None:
    """LLM 产出 SQL 后的**第二道**闸：交给 `sql_guard` 做结构校验。

    单独包一层的意义：把「模型输出必须先过 SQL 守卫」变成一句可读的、被测试钉住的调用，
    而不是散落在 agent 代码里的 `if validate_sql(...) is None: ...`。
    """
    from app.security.sql_guard import validate_sql

    validate_sql(sql)  # 不安全会抛 SQLSecurityError，正是我们要的「拦在数据库之前」


__all__ = [
    "INJECTION_RULES",
    "InjectionScan",
    "PromptInjectionError",
    "assert_no_injection",
    "assert_safe_llm_output",
    "normalize",
    "scan",
]
