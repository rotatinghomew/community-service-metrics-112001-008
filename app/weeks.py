"""ISO 周与班次时间的解析工具。"""

from __future__ import annotations

import re
from datetime import date, timedelta

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class WeekFormatError(ValueError):
    pass


def parse_week(week: str) -> tuple[int, int]:
    match = _WEEK_RE.match(week or "")
    if not match:
        raise WeekFormatError(f"无效的周标识：{week!r}，应形如 2026-W20")
    year, number = int(match.group(1)), int(match.group(2))
    if not 1 <= number <= 53:
        raise WeekFormatError(f"无效的周序号：{week!r}")
    try:
        date.fromisocalendar(year, number, 1)
    except ValueError as exc:
        raise WeekFormatError(f"无效的周标识：{week!r}") from exc
    return year, number


def week_start(week: str) -> date:
    year, number = parse_week(week)
    return date.fromisocalendar(year, number, 1)


def week_end(week: str) -> date:
    return week_start(week) + timedelta(days=6)


def week_label(day: date) -> str:
    year, number, _ = day.isocalendar()
    return f"{year}-W{number:02d}"


def shift_week(week: str, offset: int) -> str:
    return week_label(week_start(week) + timedelta(weeks=offset))


def compare_weeks(left: str, right: str) -> int:
    """按时间先后比较两个 ISO 周。"""
    lw, rw = week_start(left), week_start(right)
    return (lw > rw) - (lw < rw)


def parse_time(value: str) -> int:
    """把 HH:MM 转成当日分钟数。空值或非法值抛出 ValueError。"""
    if not isinstance(value, str):
        raise ValueError(f"无效的时间：{value!r}")
    match = _TIME_RE.match(value.strip())
    if not match:
        raise ValueError(f"无效的时间：{value!r}，应形如 09:00")
    return int(match.group(1)) * 60 + int(match.group(2))


def format_minutes(minutes: int) -> str:
    minutes = int(round(minutes)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
