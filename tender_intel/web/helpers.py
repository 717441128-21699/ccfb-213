"""模板与路由共享的小工具。"""
from __future__ import annotations

import json

from markupsafe import Markup

_STATUS = {
    "running": ("blue", "运行中"),
    "succeeded": ("green", "成功"),
    "failed": ("red", "失败"),
    "cancelled": ("amber", "已取消"),
    "active": ("green", "有效"),
    "delisted": ("gray", "已下架"),
    "cancelled_notice": ("red", "已废标"),
}

_TYPE = {
    "tender": ("blue", "招标"),
    "correction": ("purple", "更正"),
    "cancellation": ("red", "废标"),
}


def status_badge(status: str) -> Markup:
    color, text = _STATUS.get(status, ("gray", status))
    return Markup(f'<span class="badge {color}">{text}</span>')


def type_badge(notice_type: str) -> Markup:
    color, text = _TYPE.get(notice_type, ("gray", notice_type))
    return Markup(f'<span class="badge {color}">{text}</span>')


def severity_badge(severity: str) -> Markup:
    color = {"critical": "red", "warning": "amber", "info": "blue"}.get(severity, "gray")
    return Markup(f'<span class="badge {color}">{severity}</span>')


def fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    return value.replace("T", " ").replace("Z", " UTC")


def fmt_amount(value) -> str:
    if value is None:
        return "—"
    return f"¥{float(value):,.2f}"


def pretty_json(value: str | dict | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def register_helpers(app) -> None:
    app.jinja_env.globals.update(
        status_badge=status_badge,
        type_badge=type_badge,
        severity_badge=severity_badge,
        fmt_ts=fmt_ts,
        fmt_amount=fmt_amount,
        pretty_json=pretty_json,
    )
