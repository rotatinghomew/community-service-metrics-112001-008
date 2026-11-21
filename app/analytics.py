"""负荷预测、能力核算与排班建议。

核心口径（与界面「假设」面板一一对应）：

1. 到件修订先于一切计算；撤销/转派/补录只改变有效到件量，原始记录保留。
2. 历史基线取起始周之前、资料中实际出现过的最近 ``history_weeks`` 周，按近因加权；
   缺失周不等于零，只有真实录入的 0 才算 0，并降低置信度。
3. 录入延迟用区间表达，不假装能区分业务高峰与补录。
4. 积压独立核算，不并入新到件需求，避免重复计算。
5. 能力按「网格 × 技能」分别核算，技能之间不互相抵扣；同一人员的重叠班次
   只算一次可用时数，跨点重叠时段按主班次归属并列为冲突。
6. 预测与能力均输出区间；无法预测或无人可派时明确标注，不默认零需求。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from .config import Scenario
from .data import CLOSED_BACKLOG_STATUSES, DataError, Dataset
from .weeks import compare_weeks, parse_time, week_end, week_start

UNPREDICTABLE = "unpredictable"
INSUFFICIENT = "insufficient"
NO_BASELINE = frozenset({UNPREDICTABLE, INSUFFICIENT})
UNFILLABLE = "unfillable"
PARTIAL = "partial"
COVERED = "covered"
AT_RISK = "at_risk"
LOW_CONFIDENCE = "low_confidence"

DEFAULT_SHIFT_HOURS = 8.0


@dataclass
class Interval:
    low: float
    typical: float
    high: float

    def as_dict(self) -> dict[str, float]:
        return {
            "low": round(self.low, 2),
            "typical": round(self.typical, 2),
            "high": round(self.high, 2),
        }


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _weighted_mean(pairs: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in pairs)
    if total_weight <= 0:
        return 0.0
    return sum(value * weight for value, weight in pairs) / total_weight


def _weighted_std(pairs: list[tuple[float, float]], mean: float) -> float:
    total_weight = sum(weight for _, weight in pairs)
    if total_weight <= 0 or len(pairs) < 2:
        return 0.0
    variance = sum(weight * (value - mean) ** 2 for value, weight in pairs) / total_weight
    return math.sqrt(max(0.0, variance))


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _interval_minutes(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in intervals)


def _intersection_minutes(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> int:
    return sum(
        max(0, min(a_end, b_end) - max(a_start, b_start))
        for a_start, a_end in left
        for b_start, b_end in right
    )


@dataclass
class AdjustedCell:
    """某网格/类别/周经修订后的有效到件。"""

    week: str
    grid: str
    category: str
    raw_count: int
    count_low: int
    count_high: int
    entered_late: int
    observed: bool = True
    adjustments: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "week": self.week,
            "grid": self.grid,
            "category": self.category,
            "raw_count": self.raw_count,
            "count_low": self.count_low,
            "count_high": self.count_high,
            "entered_late": self.entered_late,
            "observed": self.observed,
            "adjustments": self.adjustments,
        }


def _count_bounds(count: int, entered_late: int, mode: str) -> tuple[int, int]:
    lost = min(entered_late, count)
    if mode == "include":
        return count, count
    if mode == "exclude":
        return count - lost, count - lost
    return count - lost, count


def apply_adjustments(
    dataset: Dataset, mode: str, extra: Iterable[dict[str, Any]] = ()
) -> tuple[list[AdjustedCell], list[dict[str, Any]]]:
    """把资料内与运行时的修订应用到原始到件，并按录入延迟口径给出有效件数区间。"""
    cells: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in dataset.arrivals:
        key = (record.week, record.grid, record.category)
        bucket = cells.setdefault(
            key, {"raw_count": 0, "entered_late": 0, "adjustments": [], "from_source": True}
        )
        bucket["raw_count"] += record.count
        bucket["entered_late"] += record.entered_late

    # 运行时修订先按 id 去重，避免同一条重复应用。
    merged: dict[str, dict[str, Any]] = {}
    for adjustment in dataset.adjustments:
        merged[adjustment.id] = {
            "id": adjustment.id,
            "kind": adjustment.kind,
            "week": adjustment.week,
            "grid": adjustment.grid,
            "category": adjustment.category,
            "count_delta": adjustment.count_delta,
            "to_grid": adjustment.to_grid,
            "note": adjustment.note,
            "origin": "source",
        }
    for adjustment in extra:
        merged[adjustment["id"]] = {**adjustment, "origin": adjustment.get("origin", "runtime")}

    applied_log: list[dict[str, Any]] = []
    for log in merged.values():
        delta = int(log["count_delta"])
        if log["kind"] in {"cancel", "transfer"} and delta > 0:
            raise DataError(f"到件修订 {log['id']}：撤销/转派应为非正 count_delta。")
        if log["kind"] == "supplement" and delta < 0:
            raise DataError(f"到件修订 {log['id']}：补录应为非负 count_delta。")
        targets: list[tuple[tuple[str, str, str], int, str]] = []
        if log["kind"] == "transfer":
            if not log.get("to_grid"):
                raise DataError(f"到件修订 {log['id']}：转派需要 to_grid。")
            if log["to_grid"] == log["grid"]:
                raise DataError(f"到件修订 {log['id']}：转派的目标网格与原网格相同。")
            targets.append(((log["week"], log["grid"], log["category"]), -abs(delta), "转出"))
            targets.append(((log["week"], log["to_grid"], log["category"]), abs(delta), "转入"))
        else:
            targets.append(((log["week"], log["grid"], log["category"]), delta, log["kind"]))

        for key, signed_delta, role in targets:
            bucket = cells.setdefault(
                key, {"raw_count": 0, "entered_late": 0, "adjustments": [], "from_source": False}
            )
            bucket["adjustments"].append(
                {
                    "id": log["id"],
                    "kind": log["kind"],
                    "count_delta": delta,
                    "applied_delta": signed_delta,
                    "role": role,
                    "to_grid": log.get("to_grid"),
                    "note": log.get("note"),
                    "origin": log.get("origin", "runtime"),
                }
            )

        if log["kind"] in {"cancel", "transfer"}:
            source_key = (log["week"], log["grid"], log["category"])
            source = cells.get(source_key) or {"raw_count": 0, "adjustments": []}
            net = source["raw_count"] + sum(item["applied_delta"] for item in source["adjustments"])
            if net < 0:
                raise DataError(
                    f"到件修订 {log['id']} 扣减后为负（{log['grid']}/{log['week']}/"
                    f"{log['category']}）。请核对撤销/转派数量是否超过有效到件。"
                )
        applied_log.append(
            {
                "id": log["id"],
                "kind": log["kind"],
                "week": log["week"],
                "grid": log["grid"],
                "category": log["category"],
                "count_delta": delta,
                "to_grid": log.get("to_grid"),
                "note": log.get("note"),
                "origin": log.get("origin", "runtime"),
            }
        )

    result: list[AdjustedCell] = []
    for (week, grid, category), bucket in sorted(
        cells.items(), key=lambda item: (week_start(item[0][0]), item[0][1], item[0][2])
    ):
        applied_delta = sum(item["applied_delta"] for item in bucket["adjustments"])
        raw = max(0, bucket["raw_count"] + applied_delta)
        low, high = _count_bounds(raw, bucket["entered_late"], mode)
        result.append(
            AdjustedCell(
                week=week,
                grid=grid,
                category=category,
                raw_count=raw,
                count_low=low,
                count_high=high,
                entered_late=bucket["entered_late"],
                observed=bucket["raw_count"] > 0 or applied_delta != 0 or bucket.get("from_source", False),
                adjustments=bucket["adjustments"],
            )
        )
    return result, applied_log


def _history_weeks(cells: list[AdjustedCell], scenario: Scenario, start_week: str) -> list[str]:
    weeks = sorted(
        {cell.week for cell in cells if compare_weeks(cell.week, start_week) < 0},
        key=week_start,
    )
    return weeks[-scenario.history_weeks :]


@dataclass
class Baseline:
    grid: str
    category: str
    status: str
    weeks_observed: int
    count: Interval
    confidence: str
    notes: list[str] = field(default_factory=list)
    weekly: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "grid": self.grid,
            "category": self.category,
            "status": self.status,
            "weeks_observed": self.weeks_observed,
            "count": self.count.as_dict(),
            "confidence": self.confidence,
            "notes": self.notes,
            "weekly": self.weekly,
        }


def _baseline_for(
    cells: list[AdjustedCell],
    grid: str,
    category: str,
    history_weeks: list[str],
    any_history: bool,
) -> Baseline:
    by_week = {cell.week: cell for cell in cells if cell.grid == grid and cell.category == category}
    observed = [by_week[week] for week in history_weeks if week in by_week]

    if not observed:
        if not any_history:
            return Baseline(
                grid=grid,
                category=category,
                status=UNPREDICTABLE,
                weeks_observed=0,
                count=Interval(0, 0, 0),
                confidence="none",
                notes=["资料中没有该网格/类别的任何到件记录，无法预测；这里的 0 不是零需求结论。"],
            )
        return Baseline(
            grid=grid,
            category=category,
            status=INSUFFICIENT,
            weeks_observed=0,
            count=Interval(0, 0, 0),
            confidence="none",
            notes=["该网格/类别在选定历史窗口内没有记录，无法形成基线；不按零需求处理。"],
        )

    typical_pairs, low_pairs, high_pairs = [], [], []
    for index, cell in enumerate(observed):
        recency = 1.0 + 0.5 * index
        typical_pairs.append(((cell.count_low + cell.count_high) / 2, recency))
        low_pairs.append((float(cell.count_low), recency))
        high_pairs.append((float(cell.count_high), recency))

    typical_mean = _weighted_mean(typical_pairs)
    low_mean = _weighted_mean(low_pairs)
    high_mean = _weighted_mean(high_pairs)
    std = _weighted_std(typical_pairs, typical_mean)
    notes: list[str] = []
    confidence = "normal"

    if len(observed) == 1:
        spread = max(typical_mean * 0.25, 1.0)
        low_mean = min(low_mean, typical_mean - spread)
        high_mean = max(high_mean, typical_mean + spread)
        confidence = "low"
        notes.append("历史窗口内只有 1 个观测周，区间按 ±25% 经验幅度放宽（假设，不是统计置信区间）。")
    elif len(observed) < len(history_weeks):
        confidence = "low"
        notes.append(
            f"历史窗口共 {len(history_weeks)} 周，仅 {len(observed)} 周有记录；缺失周未按 0 处理。"
        )
    else:
        low_mean = min(low_mean, typical_mean - std)
        high_mean = max(high_mean, typical_mean + std)

    if all(cell.count_low == 0 and cell.count_high == 0 for cell in observed):
        confidence = "low"
        notes.append("历史记录均为 0 件；这是真实录入的零，但样本不足以断言未来为零。")
        low_mean = 0.0

    return Baseline(
        grid=grid,
        category=category,
        status=COVERED if confidence == "normal" else LOW_CONFIDENCE,
        weeks_observed=len(observed),
        count=Interval(max(0.0, low_mean), max(0.0, typical_mean), max(0.0, high_mean)),
        confidence=confidence,
        notes=notes,
        weekly=[cell.as_dict() for cell in observed],
    )


def _calendar_events(dataset: Dataset, scenario: Scenario, week: str, grid: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if scenario.calendar_mode == "apply":
        for item in dataset.calendar:
            if item.week == week and item.grid == grid:
                spread = scenario.calendar_uncertainty
                events.append(
                    {
                        "source": "calendar",
                        "event": item.event,
                        "factor": item.factor,
                        "factor_low": round(1 + (item.factor - 1) * (1 - spread), 4),
                        "factor_high": round(1 + (item.factor - 1) * (1 + spread), 4),
                        "note": "合成情景系数，未经验证的因果估计。",
                    }
                )
    events.extend(scenario.custom_multiplier(week, grid)["events"])
    return events


def _combine_factors(events: list[dict[str, Any]]) -> dict[str, float]:
    low = typical = high = 1.0
    for event in events:
        low *= event["factor_low"]
        typical *= event["factor"]
        high *= event["factor_high"]
    return {"low": low, "typical": typical, "high": high}


def _forecast(
    dataset: Dataset,
    scenario: Scenario,
    baseline: Baseline,
    week: str,
    week_index: int,
) -> dict[str, Any]:
    events = _calendar_events(dataset, scenario, week, baseline.grid)
    factors = _combine_factors(events)
    trend = scenario.trend_factor(week_index)
    count = Interval(
        baseline.count.low * factors["low"] * trend,
        baseline.count.typical * factors["typical"] * trend,
        baseline.count.high * factors["high"] * trend,
    )
    minutes, assumed = scenario.standard_minutes(baseline.category)
    hours = Interval(
        count.low * minutes["low"] / 60.0,
        count.typical * minutes["typical"] / 60.0,
        count.high * minutes["high"] / 60.0,
    )
    status = baseline.status
    notes = list(baseline.notes)
    if assumed:
        notes.append("该类别没有专门工时假设，暂用通用工时区间；请补充标准工时。")
        if status == COVERED:
            status = LOW_CONFIDENCE
    if events:
        notes.append("已应用本周日历/情景系数；系数区间按偏离 1 的幅度附加不确定性。")
    return {
        "week": week,
        "grid": baseline.grid,
        "category": baseline.category,
        "status": status,
        "baseline_status": baseline.status,
        "confidence": baseline.confidence,
        "demand_cases": count.as_dict(),
        "demand_hours": hours.as_dict(),
        "minutes_per_case": {key: _round(value, 1) for key, value in minutes.items()},
        "assumed_minutes": assumed,
        "factors": events,
        "combined_factor": {key: _round(value, 4) for key, value in factors.items()},
        "trend_factor": _round(trend, 4),
        "notes": notes,
    }


def _skill_valid_for_week(skill, week: str) -> tuple[bool, bool]:
    """返回 (是否可用, 是否覆盖整周)。有效期仅部分覆盖时标记。"""
    start, end = week_start(week), week_end(week)
    valid_from = date.fromisoformat(skill.valid_from) if skill.valid_from else None
    valid_to = date.fromisoformat(skill.valid_to) if skill.valid_to else None
    try:
        if valid_from and valid_from > end:
            return False, False
        if valid_to and valid_to < start:
            return False, False
    except TypeError:  # pragma: no cover - 防御性
        return False, False
    full = (not valid_from or valid_from <= start) and (not valid_to or valid_to >= end)
    return True, full


@dataclass
class ShiftSupply:
    person: str
    week: str
    grid: str
    minutes: int
    gross_minutes: int
    break_minutes: int
    contested_minutes: int
    skills: list[str]
    partial_skills: list[str]
    shift_ids: list[str]
    notes: list[str] = field(default_factory=list)


def _person_grid_minutes(
    shifts: list[Any], person: str, week: str
) -> tuple[list[ShiftSupply], list[dict[str, Any]]]:
    """核算某人的班次。跨网格重叠时段只计一次并归给主班次网格。"""
    by_grid: dict[str, list[tuple[int, int]]] = {}
    break_by_grid: dict[str, int] = {}
    shift_ids_by_grid: dict[str, list[str]] = {}
    for shift in shifts:
        start, end = parse_time(shift.start), parse_time(shift.end)
        by_grid.setdefault(shift.grid, []).append((start, end))
        break_by_grid[shift.grid] = break_by_grid.get(shift.grid, 0) + shift.break_minutes
        shift_ids_by_grid.setdefault(shift.grid, []).append(shift.id)

    merged = {grid: _merge_intervals(items) for grid, items in by_grid.items()}
    spans = {grid: _interval_minutes(items) for grid, items in merged.items()}
    # 主班次：总时数最长者；并列时按网格标识稳定排序。
    primary_grid = sorted(merged, key=lambda grid: (-spans[grid], grid))[0]

    # 逐段判断同一人同时出现在哪些网格；多网格重叠归给主班次网格，保证总时数守恒。
    allocated = {grid: 0 for grid in merged}
    contested = {grid: 0 for grid in merged}
    points = sorted({point for items in merged.values() for interval in items for point in interval})
    for left, right in zip(points, points[1:]):
        if right <= left:
            continue
        active = [grid for grid, items in merged.items() if any(s <= left < e for s, e in items)]
        if not active:
            continue
        if len(active) == 1:
            allocated[active[0]] += right - left
        else:
            owner = primary_grid if primary_grid in active else sorted(active)[0]
            allocated[owner] += right - left
            for grid in active:
                contested[grid] += right - left

    supplies: list[ShiftSupply] = []
    conflicts: list[dict[str, Any]] = []
    for grid in sorted(merged):
        break_minutes = min(break_by_grid.get(grid, 0), allocated[grid])
        minutes = max(0, allocated[grid] - break_minutes)
        supplies.append(
            ShiftSupply(
                person=person,
                week=week,
                grid=grid,
                minutes=minutes,
                gross_minutes=max(0, spans[grid] - break_by_grid.get(grid, 0)),
                break_minutes=break_minutes,
                contested_minutes=contested[grid],
                skills=[],
                partial_skills=[],
                shift_ids=sorted(shift_ids_by_grid.get(grid, [])),
            )
        )
        if contested[grid]:
            conflicts.append(
                {
                    "person": person,
                    "week": week,
                    "grid": grid,
                    "minutes": contested[grid],
                    "primary_grid": primary_grid,
                    "message": (
                        f"{person} 在 {week} 的班次与其他网格重叠 {contested[grid]} 分钟；"
                        f"重叠时段归给主班次网格 {primary_grid}，其他网格不再重复计时。"
                    ),
                }
            )
    return supplies, conflicts


def _supply(
    dataset: Dataset, week: str
) -> tuple[list[ShiftSupply], list[dict[str, Any]], list[str]]:
    people: dict[str, list[Any]] = {}
    for shift in dataset.shifts:
        if shift.week == week:
            people.setdefault(shift.person, []).append(shift)

    supplies: list[ShiftSupply] = []
    conflicts: list[dict[str, Any]] = []
    warnings: list[str] = []
    for person, person_shifts in sorted(people.items()):
        person_supplies, person_conflicts = _person_grid_minutes(person_shifts, person, week)
        for supply in person_supplies:
            for skill in dataset.skills:
                if skill.person != person or skill.grid != supply.grid:
                    continue
                valid, full = _skill_valid_for_week(skill, week)
                if not valid:
                    continue
                supply.skills.append(skill.skill)
                if not full:
                    supply.partial_skills.append(skill.skill)
            supply.skills = sorted(set(supply.skills))
            supply.partial_skills = sorted(set(supply.partial_skills))
            if supply.partial_skills:
                supply.notes.append(
                    f"资质 {', '.join(supply.partial_skills)} 仅部分覆盖本周，实际可用日未细分，需人工确认。"
                )
            if not supply.skills and supply.minutes > 0:
                warnings.append(
                    f"{person} 在 {supply.grid} / {week} 有班次但没有有效资质，该班次不能计入任何技能能力。"
                )
        supplies.extend(person_supplies)
        conflicts.extend(person_conflicts)
    return supplies, conflicts, warnings


@dataclass
class CapacityCell:
    week: str
    grid: str
    skill: str
    allocated_hours: float
    gross_hours: float
    contested_hours: float
    people: list[dict[str, Any]]
    partial_skills: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def allocated(self, scenario: Scenario) -> Interval:
        low_u, point_u, high_u = scenario.effective_utilization()
        return Interval(
            self.allocated_hours * low_u,
            self.allocated_hours * point_u,
            self.allocated_hours * high_u,
        )

    def as_dict(self, scenario: Scenario) -> dict[str, Any]:
        return {
            "week": self.week,
            "grid": self.grid,
            "skill": self.skill,
            "allocated_hours": _round(self.allocated_hours),
            "gross_hours": _round(self.gross_hours),
            "contested_hours": _round(self.contested_hours),
            "deliverable_hours": self.allocated(scenario).as_dict(),
            "people": self.people,
            "partial_skills": self.partial_skills,
            "notes": self.notes,
        }


def _capacity_cells(
    supplies: list[ShiftSupply],
    weeks: list[str],
    grids: list[str],
    skills: list[str],
    demand: dict[tuple[str, str, str], float],
) -> tuple[list[CapacityCell], list[dict[str, Any]], list[str]]:
    """按同网格需求比例把人员班次分摊到技能，避免多技能重复计入能力。"""
    cells: dict[tuple[str, str, str], CapacityCell] = {}
    idle: list[dict[str, Any]] = []
    notes: list[str] = []

    for week in weeks:
        for grid in grids:
            for skill in skills:
                cells[(week, grid, skill)] = CapacityCell(
                    week=week,
                    grid=grid,
                    skill=skill,
                    allocated_hours=0.0,
                    gross_hours=0.0,
                    contested_hours=0.0,
                    people=[],
                )

    for supply in supplies:
        gross_by_skill = {skill: supply.minutes / 60.0 for skill in supply.skills}
        for skill in supply.skills:
            cell = cells[(supply.week, supply.grid, skill)]
            cell.gross_hours += gross_by_skill[skill]
            if skill in supply.partial_skills:
                cell.partial_skills.append(supply.person)
        if not supply.minutes:
            continue
        demand_skills = [skill for skill in supply.skills if demand.get((supply.week, supply.grid, skill), 0) > 0]
        if not demand_skills:
            if not supply.skills:
                reason = "该人员在此网格没有有效资质，班次时数不能计入任何技能能力。"
            else:
                reason = "该人员的资质对应需求不可预测或为零，时数未分配；不等于需求为零。"
            idle.append(
                {
                    "week": supply.week,
                    "grid": supply.grid,
                    "person": supply.person,
                    "hours": _round(supply.minutes / 60.0),
                    "skills": supply.skills,
                    "reason": reason,
                }
            )
            continue
        total_demand = sum(demand[(supply.week, supply.grid, skill)] for skill in demand_skills)
        allocated_by_skill: list[tuple[str, float]] = []
        for skill in supply.skills:
            share = demand.get((supply.week, supply.grid, skill), 0.0) / total_demand
            hours = supply.minutes / 60.0 * share
            if share > 0:
                allocated_by_skill.append((skill, hours))
            if hours <= 0:
                continue
            cell = cells[(supply.week, supply.grid, skill)]
            cell.allocated_hours += hours
            cell.people.append(
                {
                    "person": supply.person,
                    "hours": _round(hours),
                    "shift_ids": supply.shift_ids,
                    "contested_minutes": supply.contested_minutes,
                    "shared_with_skills": supply.skills,
                }
            )
        if len(allocated_by_skill) > 1:
            notes.append(
                f"{supply.person} 在 {supply.grid}/{supply.week} 同时具备 "
                f"{', '.join(supply.skills)} 资质，班次时数按需求比例分摊；"
                "某一技能的能力不是该人员的全部时数，也不应把各技能能力当作独立新增人力。"
            )

    ordered = sorted(cells.values(), key=lambda c: (week_start(c.week), c.grid, c.skill))
    return ordered, idle, notes


def _backlog_items(dataset: Dataset, scenario: Scenario) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    closed = 0
    for record in dataset.backlog:
        is_closed = record.status in CLOSED_BACKLOG_STATUSES
        is_paused = record.status in {"paused", "挂起"}
        eligible = not is_closed and (scenario.include_paused_backlog or not is_paused)
        if is_closed:
            closed += 1
        items.append(
            {
                "case": record.case,
                "grid": record.grid,
                "category": record.category,
                "estimated_hours": _round(record.estimated_hours),
                "status": record.status,
                "is_closed": is_closed,
                "is_paused": is_paused,
                "eligible_for_plan": eligible,
                "reason": (
                    "已终结，不计入积压"
                    if is_closed
                    else (
                        "挂起，当前情景未纳入消化目标"
                        if is_paused and not scenario.include_paused_backlog
                        else "未结，计入积压"
                    )
                ),
            }
        )
    items.sort(key=lambda item: (item["grid"], item["category"], item["case"]))
    return items, (
        [{"closed_cases": closed, "note": "已终结事项不计入未结积压。"}] if closed else []
    )


def _backlog_by_grid_category(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if item["is_closed"]:
            continue
        group = groups.setdefault(
            (item["grid"], item["category"]),
            {
                "grid": item["grid"],
                "category": item["category"],
                "total_hours": 0.0,
                "eligible_hours": 0.0,
                "paused_hours": 0.0,
                "cases": [],
            },
        )
        group["total_hours"] += item["estimated_hours"]
        if item["eligible_for_plan"]:
            group["eligible_hours"] += item["estimated_hours"]
        if item["is_paused"]:
            group["paused_hours"] += item["estimated_hours"]
        group["cases"].append(item["case"])
    result = []
    for group in groups.values():
        for key in ("total_hours", "eligible_hours", "paused_hours"):
            group[key] = _round(group[key])
        result.append(group)
    return sorted(result, key=lambda g: (g["grid"], g["category"]))


def _coverage_for(
    forecast: dict[str, Any],
    capacity: dict[str, Any],
) -> dict[str, Any]:
    demand = forecast["demand_hours"]
    if forecast["status"] in NO_BASELINE:
        reference = capacity["gross_hours"] if capacity else 0.0
        status = UNPREDICTABLE if forecast["status"] == UNPREDICTABLE else INSUFFICIENT
        reason = (
            "没有该网格/类别的任何历史到件，无法估计需求。"
            if status == UNPREDICTABLE
            else "该网格/类别在选定历史窗口内没有记录，无法形成基线。"
        )
        return {
            "week": forecast["week"],
            "grid": forecast["grid"],
            "category": forecast["category"],
            "status": status,
            "demand_status": forecast["status"],
            "demand_hours": None,
            "capacity_hours": None,
            "reference_capacity_hours": _round(reference),
            "gap_hours": None,
            "message": (
                reason
                + " 未按零需求处理；若确有服务事项，应先补录历史或人工给出需求假设。"
                + (" 具备资质人员的班次上限为参考值，不代表本周需要这么多。" if reference else "")
            ),
        }

    capacity_hours = capacity["deliverable_hours"] if capacity else {"low": 0.0, "typical": 0.0, "high": 0.0}
    has_people = bool(capacity and capacity["people"])
    if not has_people:
        gap = {
            "low": _round(demand["low"]),
            "typical": _round(demand["typical"]),
            "high": _round(demand["high"]),
        }
        return {
            "week": forecast["week"],
            "grid": forecast["grid"],
            "category": forecast["category"],
            "status": UNFILLABLE,
            "demand_status": forecast["status"],
            "demand_hours": demand,
            "capacity_hours": capacity_hours,
            "gap_hours": gap,
            "message": (
                "该网格/技能没有任何可兑现人员班次，需求无法满足；缺口不会被其他技能的空闲时数抵消。"
            ),
        }

    gap_low = max(0.0, demand["low"] - capacity_hours["high"])
    gap_typical = max(0.0, demand["typical"] - capacity_hours["typical"])
    gap_high = max(0.0, demand["high"] - capacity_hours["low"])
    if gap_typical <= 0 and gap_high <= 0:
        status, message = COVERED, "按点估计与悲观情形均能覆盖。"
    elif gap_typical <= 0:
        status, message = AT_RISK, "按点估计可覆盖，但需求上沿或能力下沿时会出现缺口。"
    else:
        status, message = PARTIAL, "点估计存在缺口，需要增派、调整班次或明确接受风险。"
    return {
        "week": forecast["week"],
        "grid": forecast["grid"],
        "category": forecast["category"],
        "status": status,
        "demand_status": forecast["status"],
        "demand_hours": demand,
        "capacity_hours": capacity_hours,
        "gap_hours": {"low": _round(gap_low), "typical": _round(gap_typical), "high": _round(gap_high)},
        "message": message,
    }


def _schedule(
    capacity_cells: list[dict[str, Any]],
    idle: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    forecasts: list[dict[str, Any]],
) -> dict[str, Any]:
    demand = {
        (item["week"], item["grid"], item["category"]): item["demand_hours"]["typical"]
        for item in forecasts
        if item["status"] not in NO_BASELINE
    }
    assignments = []
    idle = list(idle)
    for cell in capacity_cells:
        key = (cell["week"], cell["grid"], cell["skill"])
        if key not in demand:
            continue  # 不可预测的单元不产生分配，也不冒称零需求
        remaining = demand[key]
        for person in cell["people"]:
            if remaining <= 1e-9:
                idle.append(
                    {
                        "week": cell["week"],
                        "grid": cell["grid"],
                        "person": person["person"],
                        "hours": _round(person["hours"]),
                        "skills": person["shared_with_skills"],
                        "reason": "该技能的可预测需求已被其他班次覆盖，剩余时数未分配。",
                    }
                )
                continue
            hours = min(person["hours"], remaining)
            remaining -= hours
            assignments.append(
                {
                    "week": cell["week"],
                    "grid": cell["grid"],
                    "skill": cell["skill"],
                    "person": person["person"],
                    "hours": _round(hours, 1),
                    "available_hours": _round(person["hours"], 1),
                    "shift_ids": person["shift_ids"],
                    "basis": "固定班次内按需求分摊的可用时数；分配量不超过该技能点估计需求",
                }
            )
            if person["hours"] - hours > 1e-9:
                idle.append(
                    {
                        "week": cell["week"],
                        "grid": cell["grid"],
                        "person": person["person"],
                        "hours": _round(person["hours"] - hours),
                        "skills": person["shared_with_skills"],
                        "reason": "该技能点估计需求已覆盖，剩余班次时数未分配。",
                    }
                )
    assignments.sort(key=lambda item: (week_start(item["week"]), item["grid"], item["skill"], item["person"]))

    # 缺口所需增派量：只给时数与参考班次数，不虚构具体人员。
    additional = []
    for item in coverage:
        gap = item["gap_hours"] or {}
        if item["status"] in NO_BASELINE or gap.get("typical", 0) <= 0:
            continue
        typical_shifts = math.ceil((gap["typical"] - 1e-9) / DEFAULT_SHIFT_HOURS)
        low_shifts = math.ceil((gap["low"] - 1e-9) / DEFAULT_SHIFT_HOURS) if gap["low"] > 0 else 0
        high_shifts = math.ceil((gap["high"] - 1e-9) / DEFAULT_SHIFT_HOURS) if gap["high"] > 0 else 0
        unfillable = item["status"] == UNFILLABLE
        additional.append(
            {
                "week": item["week"],
                "grid": item["grid"],
                "skill": item["category"],
                "gap_hours": gap,
                "reference_shift_hours": DEFAULT_SHIFT_HOURS,
                "shifts_needed": {"low": low_shifts, "typical": typical_shifts, "high": high_shifts},
                "unfillable": unfillable,
                "note": (
                    "当前没有该技能的有效资质人员：这是无法满足的服务需求，不是可排班量。"
                    if unfillable
                    else "参考班次按 8 小时折算，仅为量级参考；新增班次仍须满足技能有效期与不跨点约束。"
                ),
            }
        )

    idle_sorted = sorted(
        {(item["week"], item["grid"], item["person"], item["reason"]): item for item in idle}.values(),
        key=lambda item: (week_start(item["week"]), item["grid"], item["person"]),
    )
    return {
        "assignments": assignments,
        "idle_capacity": idle_sorted,
        "additional_shifts": additional,
    }


def _fingerprint(dataset: Dataset, scenario: Scenario, extra_adjustments: list[dict[str, Any]] | None = None) -> str:
    payload = {
        "arrivals": [
            [a.id, a.grid, a.week, a.category, a.count, a.entered_late] for a in dataset.arrivals
        ],
        "adjustments": [
            [a.id, a.kind, a.grid, a.week, a.category, a.count_delta, a.to_grid]
            for a in dataset.adjustments
        ]
        + [
            [
                a["id"],
                a["kind"],
                a["grid"],
                a["week"],
                a["category"],
                a["count_delta"],
                a.get("to_grid"),
            ]
            for a in (extra_adjustments or [])
        ],
        "backlog": [
            [b.case, b.grid, b.category, b.estimated_hours, b.status] for b in dataset.backlog
        ],
        "skills": [
            [s.person, s.grid, s.skill, s.valid_from, s.valid_to] for s in dataset.skills
        ],
        "shifts": [
            [s.id, s.person, s.week, s.grid, s.start, s.end, s.break_minutes] for s in dataset.shifts
        ],
        "calendar": [[c.week, c.grid, c.event, c.factor] for c in dataset.calendar],
        "scenario": {key: value for key, value in scenario.to_dict().items() if key != "label"},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def analyse(
    dataset: Dataset,
    scenario: Scenario,
    extra_adjustments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    extra_adjustments = list(extra_adjustments or [])
    latest_arrival = dataset.latest_arrival_week()
    first_shift = dataset.first_shift_week()
    start = scenario.resolved_start_week(latest_arrival, first_shift)
    weeks = scenario.future_weeks(start)

    cells, adjustment_log = apply_adjustments(dataset, scenario.late_entry_mode, extra_adjustments)
    history_weeks = _history_weeks(cells, scenario, start)
    grids = dataset.grids
    categories = dataset.categories

    baselines = []
    for grid in grids:
        for category in categories:
            any_history = any(cell.grid == grid and cell.category == category for cell in cells)
            baselines.append(_baseline_for(cells, grid, category, history_weeks, any_history))

    forecasts: list[dict[str, Any]] = []
    for week_index, week in enumerate(weeks):
        for baseline in baselines:
            forecasts.append(_forecast(dataset, scenario, baseline, week, week_index))
    demand_typical = {
        (item["week"], item["grid"], item["category"]): item["demand_hours"]["typical"]
        for item in forecasts
    }

    supplies: list[ShiftSupply] = []
    conflicts: list[dict[str, Any]] = []
    supply_warnings: list[str] = []
    for week in weeks:
        week_supplies, week_conflicts, week_warnings = _supply(dataset, week)
        supplies.extend(week_supplies)
        conflicts.extend(week_conflicts)
        supply_warnings.extend(week_warnings)

    capacity_cells, idle, capacity_notes = _capacity_cells(
        supplies, weeks, grids, categories, demand_typical
    )
    capacity_dicts = [cell.as_dict(scenario) for cell in capacity_cells]
    capacity_index = {(cell["week"], cell["grid"], cell["skill"]): cell for cell in capacity_dicts}

    coverage = [
        _coverage_for(forecast, capacity_index[(forecast["week"], forecast["grid"], forecast["category"])])
        for forecast in forecasts
    ]

    backlog_items, backlog_notes = _backlog_items(dataset, scenario)
    backlog_groups = _backlog_by_grid_category(backlog_items)
    backlog_targets: list[dict[str, Any]] = []
    for group in backlog_groups:
        target_total = group["eligible_hours"] * scenario.backlog_target_pct
        per_week = target_total / len(weeks) if weeks else 0.0
        for week in weeks:
            capacity = capacity_index.get((week, group["grid"], group["category"]))
            deliverable = capacity["deliverable_hours"]["typical"] if capacity else 0.0
            demand = next(
                (
                    item["demand_hours"]["typical"]
                    for item in forecasts
                    if item["week"] == week
                    and item["grid"] == group["grid"]
                    and item["category"] == group["category"]
                ),
                0.0,
            )
            room = max(0.0, deliverable - demand)
            backlog_targets.append(
                {
                    "week": week,
                    "grid": group["grid"],
                    "category": group["category"],
                    "target_hours": _round(per_week),
                    "capacity_after_new_demand_hours": _round(room),
                    "coverable_hours": _round(min(per_week, room)),
                    "shortfall_hours": _round(max(0.0, per_week - room)),
                    "note": "积压独立于新到件需求；仅使用满足本周新需求后的剩余能力。",
                }
            )

    schedule = _schedule(capacity_dicts, idle, coverage, forecasts)

    gaps = []
    gap_index: dict[tuple[str, str], dict[str, float]] = {}
    for item in coverage:
        key = (item["week"], item["category"])
        bucket = gap_index.setdefault(
            key,
            {"gap_typical": 0.0, "gap_high": 0.0, "unmet_typical": 0.0, "unmet_high": 0.0},
        )
        if item["gap_hours"]:
            bucket["gap_typical"] += item["gap_hours"]["typical"]
            bucket["gap_high"] += item["gap_hours"]["high"]
        if item["status"] == UNFILLABLE:
            bucket["unmet_typical"] += item["demand_hours"]["typical"]
            bucket["unmet_high"] += item["demand_hours"]["high"]
    for (week, skill), values in sorted(gap_index.items(), key=lambda item: (week_start(item[0][0]), item[0][1])):
        if values["gap_high"] > 0 or values["unmet_high"] > 0:
            gaps.append(
                {
                    "week": week,
                    "skill": skill,
                    "gap_hours": _round(values["gap_typical"]),
                    "worst_case_gap_hours": _round(values["gap_high"]),
                    "unmet_demand_hours": _round(values["unmet_typical"]),
                    "unmet_demand_high_hours": _round(values["unmet_high"]),
                    "skill_separable": True,
                }
            )

    warnings = list(dataset.warnings) + capacity_notes + supply_warnings
    warnings.extend(note["message"] for note in conflicts)
    for forecast in forecasts:
        if forecast["status"] in NO_BASELINE:
            warnings.append(
                f"{forecast['grid']}/{forecast['category']} 在 {forecast['week']} 的需求不可预测，未按 0 计入合计。"
            )

    summary = _summary(weeks, forecasts, coverage, backlog_items)

    return {
        "meta": {
            "start_week": start,
            "weeks": weeks,
            "history_weeks": history_weeks,
            "grids": grids,
            "categories": categories,
            "generated_from": "fixtures",
            "input_fingerprint": _fingerprint(dataset, scenario, extra_adjustments),
            "assumptions": scenario.describe(),
            "warnings": sorted(set(warnings)),
            "utilization": {
                "low": _round(scenario.effective_utilization()[0], 3),
                "typical": _round(scenario.utilization, 3),
                "high": _round(scenario.effective_utilization()[2], 3),
            },
        },
        "scenario": scenario.to_dict(),
        "summary": summary,
        "history": {
            "weeks": history_weeks,
            "cells": [cell.as_dict() for cell in cells],
            "baselines": [baseline.as_dict() for baseline in baselines],
            "adjustments": adjustment_log,
        },
        "forecast": forecasts,
        "capacity": capacity_dicts,
        "conflicts": conflicts,
        "coverage": coverage,
        "gaps": gaps,
        "backlog": {
            "items": backlog_items,
            "by_grid_category": backlog_groups,
            "targets": backlog_targets,
            "notes": backlog_notes,
        },
        "schedule": schedule,
        "notes": sorted(set(warnings)),
    }


def _summary(
    weeks: list[str],
    forecasts: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    backlog_items: list[dict[str, Any]],
) -> dict[str, Any]:
    predictable = [item for item in forecasts if item["status"] not in NO_BASELINE]
    unpredictable = len(forecasts) - len(predictable)
    demand = {
        "low": _round(sum(item["demand_hours"]["low"] for item in predictable)),
        "typical": _round(sum(item["demand_hours"]["typical"] for item in predictable)),
        "high": _round(sum(item["demand_hours"]["high"] for item in predictable)),
    }
    deliverable = {
        "low": _round(sum(item["capacity_hours"]["low"] for item in coverage if item["capacity_hours"])),
        "typical": _round(sum(item["capacity_hours"]["typical"] for item in coverage if item["capacity_hours"])),
        "high": _round(sum(item["capacity_hours"]["high"] for item in coverage if item["capacity_hours"])),
    }
    gap = {
        "low": _round(sum(item["gap_hours"]["low"] for item in coverage if item["gap_hours"])),
        "typical": _round(sum(item["gap_hours"]["typical"] for item in coverage if item["gap_hours"])),
        "high": _round(sum(item["gap_hours"]["high"] for item in coverage if item["gap_hours"])),
    }
    unfillable_demand = {
        "typical": _round(
            sum(item["demand_hours"]["typical"] for item in coverage if item["status"] == UNFILLABLE)
        ),
        "high": _round(
            sum(item["demand_hours"]["high"] for item in coverage if item["status"] == UNFILLABLE)
        ),
    }
    at_risk = [item for item in coverage if item["status"] == AT_RISK]
    partial = [item for item in coverage if item["status"] == PARTIAL]
    unfillable = [item for item in coverage if item["status"] == UNFILLABLE]
    backlog_open = [item for item in backlog_items if not item["is_closed"]]
    backlog_eligible = [item for item in backlog_open if item["eligible_for_plan"]]
    return {
        "demand_hours": demand,
        "deliverable_hours": deliverable,
        "gap_hours": gap,
        "unfillable_demand_hours": unfillable_demand,
        "backlog_hours": _round(sum(item["estimated_hours"] for item in backlog_open)),
        "backlog_eligible_hours": _round(sum(item["estimated_hours"] for item in backlog_eligible)),
        "unpredictable_cells": unpredictable,
        "covered_cells": sum(1 for item in coverage if item["status"] == COVERED),
        "at_risk_cells": len(at_risk),
        "partial_cells": len(partial),
        "unfillable_cells": len(unfillable),
        "weeks": len(weeks),
        "note": "合计仅含可预测单元；不可预测的需求不按 0 计入，单列显示。",
    }


def diff_plan(confirmed: list[dict[str, Any]], recommended: list[dict[str, Any]]) -> dict[str, Any]:
    """比较已确认班表与当前建议，结论只读，不会自动覆盖已确认内容。"""

    def key(item: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            item.get("week", ""),
            item.get("grid", ""),
            item.get("skill", ""),
            item.get("person", ""),
        )

    confirmed_map = {key(item): item for item in confirmed}
    recommended_map = {key(item): item for item in recommended}
    added = [recommended_map[k] for k in recommended_map.keys() - confirmed_map.keys()]
    removed = [confirmed_map[k] for k in confirmed_map.keys() - recommended_map.keys()]
    changed = []
    for shared in confirmed_map.keys() & recommended_map.keys():
        before = float(confirmed_map[shared].get("hours", 0))
        after = float(recommended_map[shared].get("hours", 0))
        if abs(before - after) >= 0.05:
            changed.append(
                {
                    "week": shared[0],
                    "grid": shared[1],
                    "skill": shared[2],
                    "person": shared[3],
                    "confirmed_hours": _round(before, 1),
                    "recommended_hours": _round(after, 1),
                }
            )
    return {
        "has_changes": bool(added or removed or changed),
        "added": sorted(added, key=key),
        "removed": sorted(removed, key=key),
        "changed": sorted(changed, key=lambda item: (item["week"], item["grid"], item["skill"], item["person"])),
        "unchanged_count": len(confirmed_map.keys() & recommended_map.keys()) - len(changed),
    }
