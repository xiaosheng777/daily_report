from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def today_iso() -> str:
    return datetime.now(SHANGHAI_TIMEZONE).date().isoformat()


def now_iso() -> str:
    return datetime.now(SHANGHAI_TIMEZONE).replace(tzinfo=None).isoformat(timespec="seconds")


def parse_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty date")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    parts = text.replace("/", "-").split("-")
    if len(parts) == 3:
        y, m, d = [int(x) for x in parts]
        return date(y, m, d).isoformat()
    raise ValueError(f"invalid date: {value}")


def days_between(a: str, b: str) -> int:
    da = datetime.strptime(parse_date(a), "%Y-%m-%d").date()
    db = datetime.strptime(parse_date(b), "%Y-%m-%d").date()
    return abs((da - db).days)


def ym(value: str) -> str:
    return parse_date(value)[:7]
