"""公告详情解析：站点选择器可配置，解析失败逐条登记。

改版（选择器失效）时核心字段会大面积为空，由引擎统计空值比例触发告警；
单条解析失败只进 parse_failures 表，不丢公告本体。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..clock import parse_amount, parse_deadline

logger = logging.getLogger(__name__)

CORE_FIELDS = ("purchaser", "budget_amount", "deadline_at", "project_code")


@dataclass
class ParseResult:
    title: str | None = None
    purchaser: str | None = None
    budget_amount: float | None = None
    deadline_at: str | None = None
    project_code: str | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)
    empty_core: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _strip_label(text: str | None) -> str:
    if not text:
        return ""
    t = text.strip()
    # 去掉「采购单位：」「预算金额：」这类中文标签
    for sep in ("：", ":"):
        if sep in t:
            t = t.split(sep, 1)[1]
            break
    return t.strip()


def parse_detail_html(html: str, selectors: dict[str, str]) -> ParseResult:
    from .mini_dom import text_of as dom_text

    result = ParseResult()

    def text_of(key: str) -> str:
        sel = selectors.get(key)
        return dom_text(html, sel) if sel else ""

    title = text_of("detail_title")
    result.title = title or None

    purchaser_raw = _strip_label(text_of("detail_purchaser"))
    result.purchaser = purchaser_raw or None
    if not purchaser_raw:
        result.failures.append({"field": "purchaser", "raw_value": None,
                                "reason": "selector missing or empty"})
        result.empty_core.append("purchaser")

    code_raw = _strip_label(text_of("detail_code"))
    result.project_code = code_raw or None
    if not code_raw:
        result.failures.append({"field": "project_code", "raw_value": None,
                                "reason": "selector missing or empty"})
        result.empty_core.append("project_code")

    budget_raw = _strip_label(text_of("detail_budget"))
    amount, amount_err = parse_amount(budget_raw)
    result.budget_amount = amount
    if amount_err:
        result.failures.append({"field": "amount", "raw_value": budget_raw,
                                "reason": amount_err})
        result.empty_core.append("budget_amount")

    deadline_raw = _strip_label(text_of("detail_deadline"))
    deadline, deadline_err = parse_deadline(deadline_raw)
    result.deadline_at = deadline
    if deadline_err:
        result.failures.append({"field": "deadline", "raw_value": deadline_raw,
                                "reason": deadline_err})
        result.empty_core.append("deadline_at")

    return result
