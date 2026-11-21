"""合成业务资料的读取、校验与规范化。

设计口径：
* 空字符串表示来源未给出该事实，调用方必须区分「未知」与「零」。
* 未结事项的 ``paused`` 等状态不是终态，仍占用积压；只有明确的撤销/完成状态才移出。
* 到件事项被撤销、转派或补录后，通过 ``arrival_adjustments`` 修订历史到件，
  原始记录保留可追溯。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .weeks import WeekFormatError, compare_weeks, parse_time, parse_week


class DataError(ValueError):
    """资料不完整或相互矛盾。"""


def _require(record: dict, key: str, table: str) -> Any:
    if key not in record:
        raise DataError(f"{table} 记录缺少字段 {key!r}：{record!r}")
    return record[key]


def _as_text(record: dict, key: str, table: str) -> str:
    value = _require(record, key, table)
    if not isinstance(value, str):
        raise DataError(f"{table}.{key} 应为文本：{record!r}")
    return value.strip()


def _as_optional_text(record: dict, key: str, table: str) -> str | None:
    value = _as_text(record, key, table)
    return value or None


def _as_int(record: dict, key: str, table: str, minimum: int = 0) -> int:
    value = _require(record, key, table)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError(f"{table}.{key} 应为整数：{record!r}")
    if value < minimum:
        raise DataError(f"{table}.{key} 不能小于 {minimum}：{record!r}")
    return value


def _as_number(record: dict, key: str, table: str, minimum: float = 0.0) -> float:
    value = _require(record, key, table)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"{table}.{key} 应为数值：{record!r}")
    if value < minimum:
        raise DataError(f"{table}.{key} 不能小于 {minimum}：{record!r}")
    return float(value)


def _as_week(record: dict, key: str, table: str) -> str:
    value = _as_text(record, key, table)
    try:
        parse_week(value)
    except WeekFormatError as exc:
        raise DataError(f"{table}.{key}：{exc}") from exc
    return value


@dataclass(frozen=True)
class ArrivalRecord:
    id: str
    grid: str
    week: str
    category: str
    count: int
    entered_late: int


@dataclass(frozen=True)
class ArrivalAdjustment:
    """对历史到件的修订。kind 取 cancel / transfer / supplement；supplement 为补录。

    transfer 需要提供 ``to_grid``（转入网格）。修订按 ``grid``/``week``/``category``
    定位原记录，``count_delta`` 表示净变化：撤销为负数，补录为正数。
    """

    id: str
    grid: str
    week: str
    category: str
    count_delta: int
    kind: str
    to_grid: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class BacklogRecord:
    case: str
    grid: str
    category: str
    estimated_hours: float
    status: str


@dataclass(frozen=True)
class SkillRecord:
    person: str
    grid: str
    skill: str
    valid_from: str
    valid_to: str | None


@dataclass(frozen=True)
class ShiftRecord:
    id: str
    person: str
    week: str
    grid: str
    start: str
    end: str
    break_minutes: int


@dataclass(frozen=True)
class CalendarRecord:
    week: str
    grid: str
    event: str
    factor: float


# 明确的终态：不再计入积压。paused / in_progress / pending 均仍在队列。
CLOSED_BACKLOG_STATUSES = frozenset({"closed", "done", "cancelled", "canceled", "resolved", "已结", "已撤销"})
KNOWN_BACKLOG_STATUSES = CLOSED_BACKLOG_STATUSES | frozenset(
    {"open", "paused", "in_progress", "pending", "assigned", "待处理", "进行中", "挂起"}
)
ADJUSTMENT_KINDS = frozenset({"cancel", "transfer", "supplement"})


@dataclass
class Dataset:
    arrivals: list[ArrivalRecord] = field(default_factory=list)
    backlog: list[BacklogRecord] = field(default_factory=list)
    skills: list[SkillRecord] = field(default_factory=list)
    shifts: list[ShiftRecord] = field(default_factory=list)
    calendar: list[CalendarRecord] = field(default_factory=list)
    adjustments: list[ArrivalAdjustment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def grids(self) -> list[str]:
        found = {
            *(a.grid for a in self.arrivals),
            *(b.grid for b in self.backlog),
            *(s.grid for s in self.skills),
            *(s.grid for s in self.shifts),
            *(c.grid for c in self.calendar),
        }
        return sorted(grid for grid in found if grid)

    @property
    def categories(self) -> list[str]:
        found = {
            *(a.category for a in self.arrivals),
            *(b.category for b in self.backlog),
            *(s.skill for s in self.skills),
        }
        return sorted(category for category in found if category)

    @property
    def weeks(self) -> list[str]:
        found = {*(a.week for a in self.arrivals if a.week), *(s.week for s in self.shifts if s.week)}
        return sorted(found, key=lambda w: parse_week(w))

    def latest_arrival_week(self) -> str | None:
        return max((a.week for a in self.arrivals), key=parse_week, default=None)

    def first_shift_week(self) -> str | None:
        return min((s.week for s in self.shifts), key=parse_week, default=None)


def _load_json(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(f"找不到资料文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"{path.name} 不是有效 JSON：{exc}") from exc
    if isinstance(raw, dict) and "records" in raw:
        raw = raw["records"]
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise DataError(f"{path.name} 应为记录数组")
    return raw


def _parse_arrivals(records: Iterable[dict]) -> list[ArrivalRecord]:
    table = "arrivals"
    result, seen = [], set()
    for record in records:
        item = ArrivalRecord(
            id=_as_text(record, "id", table),
            grid=_as_text(record, "grid", table),
            week=_as_week(record, "week", table),
            category=_as_text(record, "category", table),
            count=_as_int(record, "count", table),
            entered_late=_as_int(record, "entered_late", table),
        )
        if item.id in seen:
            raise DataError(f"arrivals 中 id 重复：{item.id}")
        if item.entered_late > item.count:
            raise DataError(f"arrivals {item.id} 的 entered_late 大于 count")
        if not item.grid or not item.category:
            raise DataError(f"arrivals {item.id} 的 grid/category 不能为空")
        seen.add(item.id)
        result.append(item)
    return sorted(result, key=lambda a: (a.week, a.grid, a.category, a.id))


def _parse_adjustments(records: Iterable[dict]) -> list[ArrivalAdjustment]:
    table = "arrival_adjustments"
    result, seen = [], set()
    for record in records:
        kind = _as_text(record, "kind", table)
        if kind not in ADJUSTMENT_KINDS:
            raise DataError(f"{table} 记录 {record.get('id')!r} 的 kind 无效：{kind!r}")
        item = ArrivalAdjustment(
            id=_as_text(record, "id", table),
            grid=_as_text(record, "grid", table),
            week=_as_week(record, "week", table),
            category=_as_text(record, "category", table),
            count_delta=_as_int(record, "count_delta", table, minimum=-1_000_000),
            kind=kind,
            to_grid=_as_optional_text(record, "to_grid", table),
            note=_as_optional_text(record, "note", table),
        )
        if item.id in seen:
            raise DataError(f"{table} 中 id 重复：{item.id}")
        if kind in {"cancel", "transfer"} and item.count_delta > 0:
            raise DataError(f"{table} {item.id}：撤销/转派应为非正 count_delta")
        if kind == "supplement" and item.count_delta < 0:
            raise DataError(f"{table} {item.id}：补录应为非负 count_delta")
        if kind == "transfer" and not item.to_grid:
            raise DataError(f"{table} {item.id}：转派需要 to_grid")
        if item.to_grid and item.to_grid == item.grid:
            raise DataError(f"{table} {item.id}：转派的目标网格与原网格相同")
        seen.add(item.id)
        result.append(item)
    return result


def _parse_backlog(records: Iterable[dict], warnings: list[str]) -> list[BacklogRecord]:
    table = "backlog"
    result, seen = [], set()
    for record in records:
        item = BacklogRecord(
            case=_as_text(record, "case", table),
            grid=_as_text(record, "grid", table),
            category=_as_text(record, "category", table),
            estimated_hours=_as_number(record, "estimated_hours", table),
            status=_as_text(record, "status", table).lower(),
        )
        if item.case in seen:
            raise DataError(f"backlog 中 case 重复：{item.case}")
        if not item.status:
            raise DataError(f"backlog {item.case} 缺少 status，无法判断是否为未结")
        if item.status not in KNOWN_BACKLOG_STATUSES:
            warnings.append(f"未结事项 {item.case} 的状态 {item.status!r} 未识别，按仍占用积压处理。")
        seen.add(item.case)
        result.append(item)
    return result


def _parse_skills(records: Iterable[dict]) -> list[SkillRecord]:
    table = "skills"
    result = []
    for record in records:
        valid_from = _as_text(record, "valid_from", table)
        valid_to = _as_optional_text(record, "valid_to", table)
        item = SkillRecord(
            person=_as_text(record, "person", table),
            grid=_as_text(record, "grid", table),
            skill=_as_text(record, "skill", table),
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if not item.grid or not item.skill or not item.person:
            raise DataError(f"{table} 记录含空 person/grid/skill：{record!r}")
        result.append(item)
    return result


def _parse_shifts(records: Iterable[dict]) -> list[ShiftRecord]:
    table = "shifts"
    result, seen = [], set()
    for record in records:
        start, end = _as_text(record, "start", table), _as_text(record, "end", table)
        try:
            start_min, end_min = parse_time(start), parse_time(end)
        except ValueError as exc:
            raise DataError(f"{table} 记录 {record.get('id')!r}：{exc}") from exc
        if end_min <= start_min:
            raise DataError(f"{table} 记录 {record.get('id')!r} 的结束时间不晚于开始时间")
        break_minutes = _as_int(record, "break_minutes", table)
        if break_minutes >= end_min - start_min:
            raise DataError(f"{table} 记录 {record.get('id')!r} 的休息时长覆盖了整个班次")
        item = ShiftRecord(
            id=_as_text(record, "id", table),
            person=_as_text(record, "person", table),
            week=_as_week(record, "week", table),
            grid=_as_text(record, "grid", table),
            start=start,
            end=end,
            break_minutes=break_minutes,
        )
        if item.id in seen:
            raise DataError(f"shifts 中 id 重复：{item.id}")
        if not item.grid or not item.person:
            raise DataError(f"{table} {item.id} 的 person/grid 不能为空")
        seen.add(item.id)
        result.append(item)
    return result


def _parse_calendar(records: Iterable[dict]) -> list[CalendarRecord]:
    table = "calendar"
    result = []
    for record in records:
        item = CalendarRecord(
            week=_as_week(record, "week", table),
            grid=_as_text(record, "grid", table),
            event=_as_text(record, "event", table),
            factor=_as_number(record, "factor", table, minimum=0.0),
        )
        if not item.grid or not item.event:
            raise DataError(f"{table} 记录含空 grid/event：{record!r}")
        if item.factor == 0:
            raise DataError(f"{table} {item.grid}/{item.week} 的 factor 为 0，会静默抹平需求")
        result.append(item)
    return result


def load_dataset(root: Path | str) -> Dataset:
    root = Path(root)
    warnings: list[str] = []
    dataset = Dataset(
        arrivals=_parse_arrivals(_load_json(root / "arrivals.json")),
        backlog=_parse_backlog(_load_json(root / "backlog.json"), warnings),
        skills=_parse_skills(_load_json(root / "skills.json")),
        shifts=_parse_shifts(_load_json(root / "shifts.json")),
        calendar=_parse_calendar(_load_json(root / "calendar.json")),
        adjustments=_parse_adjustments(_load_json(root / "arrival_adjustments.json"))
        if (root / "arrival_adjustments.json").exists()
        else [],
        warnings=warnings,
    )
    _cross_check(dataset)
    return dataset


def _cross_check(dataset: Dataset) -> None:
    """跨表一致性检查：无法解释的业务事实应报错或留下警告，不能静默归零。"""
    known_skill = {(s.person, s.grid, s.skill) for s in dataset.skills}
    known_people = {s.person for s in dataset.shifts}

    for record in dataset.skills:
        if record.person not in known_people:
            dataset.warnings.append(f"资质人员 {record.person} 没有任何班次记录，能力为 0。")
        if record.valid_to and record.valid_to < record.valid_from:
            raise DataError(f"资质 {record.person}/{record.skill} 的有效期起止颠倒")
        if record.person in known_people and record.grid not in {
            s.grid for s in dataset.shifts if s.person == record.person
        }:
            dataset.warnings.append(
                f"资质 {record.person}/{record.grid}/{record.skill} 所在网格没有该人员班次，可能无法兑付。"
            )

    for shift in dataset.shifts:
        if shift.person not in {s.person for s in dataset.skills}:
            dataset.warnings.append(f"班次 {shift.id} 的人员 {shift.person} 没有资质记录，不能用于任何技能需求。")

    arrival_weeks = {a.week for a in dataset.arrivals}
    for adjustment in dataset.adjustments:
        if adjustment.week not in arrival_weeks:
            dataset.warnings.append(
                f"到件修订 {adjustment.id} 指向的 {adjustment.week}/{adjustment.grid}/"
                f"{adjustment.category} 没有原始到件记录。"
            )

    for calendar in dataset.calendar:
        if calendar.week not in arrival_weeks:
            dataset.warnings.append(f"日历事件 {calendar.event}@{calendar.week} 对应的周没有到件记录。")

    for backlog in dataset.backlog:
        if (backlog.grid, backlog.category) not in {
            (a.grid, a.category) for a in dataset.arrivals
        } and (backlog.grid, backlog.category) not in known_skill:
            dataset.warnings.append(
                f"未结事项 {backlog.case} 的 {backlog.grid}/{backlog.category} 既无到件历史也无对应技能。"
            )

    for shift in dataset.shifts:
        if shift.grid not in {a.grid for a in dataset.arrivals} and shift.grid not in {
            c.grid for c in dataset.calendar
        }:
            dataset.warnings.append(f"班次 {shift.id} 所在网格 {shift.grid} 没有到件历史。")


def sorted_weeks(weeks: Iterable[str]) -> list[str]:
    return sorted(set(weeks), key=lambda w: parse_week(w))


def week_in_range(week: str, start: str, end: str) -> bool:
    return compare_weeks(week, start) >= 0 and compare_weeks(week, end) <= 0
