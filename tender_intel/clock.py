"""时间与基础解析工具。

系统字段（抓取时间、入库时间）用真实 UTC 时间；
mock 真值里的发布时间用模拟时钟，两者互不干扰。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


# ---------------- 金额解析 ----------------
_AMOUNT_WAN = re.compile(r"([0-9][0-9,]*(?:\.\d+)?)\s*万元")
_AMOUNT_YI = re.compile(r"([0-9][0-9,]*(?:\.\d+)?)\s*亿元")
_AMOUNT_PLAIN = re.compile(r"([0-9][0-9,]*(?:\.\d+)?)\s*(?:元|人民币|RMB|CNY)?", re.I)


def parse_amount(text: str | None) -> tuple[float | None, str | None]:
    """返回（元为单位的金额, 失败原因）。支持 万元/亿元/带千分位的元。"""
    if not text or not text.strip():
        return None, "empty"
    t = text.strip()
    try:
        if m := _AMOUNT_YI.search(t):
            return round(float(m.group(1).replace(",", "")) * 100_000_000, 2), None
        if m := _AMOUNT_WAN.search(t):
            return round(float(m.group(1).replace(",", "")) * 10_000, 2), None
        if m := _AMOUNT_PLAIN.search(t):
            return round(float(m.group(1).replace(",", "")), 2), None
    except ValueError:
        return None, f"unparsable amount: {t[:40]}"
    return None, f"unparsable amount: {t[:40]}"


# ---------------- 截止时间解析 ----------------
_CN_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
                      r"(?:\s*(\d{1,2})\s*[:：点]\s*(\d{1,2})\s*分?)?")


def parse_deadline(text: str | None) -> tuple[str | None, str | None]:
    """返回（ISO8601 字符串, 失败原因）。"""
    if not text or not text.strip():
        return None, "empty"
    t = text.strip()
    iso = parse_iso(t)
    if iso:
        return to_iso(iso), None
    m = _CN_DATE.search(t)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4) or 0)
        minute = int(m.group(5) or 0)
        try:
            return to_iso(datetime(year, month, day, hour, minute, tzinfo=timezone.utc)), None
        except ValueError:
            return None, f"bad date components: {t[:40]}"
    return None, f"unparsable deadline: {t[:40]}"


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", (title or "").lower())
