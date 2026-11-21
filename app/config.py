"""情景参数：所有可调整假设的唯一来源。

默认值都是合成情景假设，不代表已验证的业务标准。界面会逐条标注来源与影响。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .weeks import WeekFormatError, parse_week, shift_week


class ScenarioError(ValueError):
    pass


# 每类事项的直接办理工时（分钟）。来源资料没有提供标准工时，这里是显式假设。
DEFAULT_SERVICE_MINUTES: dict[str, dict[str, int]] = {
    "visit": {"low": 30, "typical": 45, "high": 75},
    "repair": {"low": 60, "typical": 90, "high": 150},
    "support": {"low": 20, "typical": 30, "high": 60},
}
UNKNOWN_CATEGORY_MINUTES = {"low": 20, "typical": 45, "high": 90}

LATE_ENTRY_MODES = {"range", "include", "exclude"}
CALENDAR_MODES = {"apply", "ignore"}
MAX_HORIZON = 12
MAX_HISTORY = 12


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ScenarioError(f"{name} 应为有限数值")
    return float(value)


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError(f"{name} 应为整数")
    return value


def _text(value: Any, name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ScenarioError(f"{name} 应为文本")
    value = value.strip()
    if not value and not allow_empty:
        raise ScenarioError(f"{name} 不能为空")
    return value


@dataclass
class Scenario:
    start_week: str = ""
    horizon_weeks: int = 2
    history_weeks: int = 4
    late_entry_mode: str = "range"
    weekly_trend_pct: float = 0.0
    service_minutes: dict[str, dict[str, float]] = field(default_factory=dict)
    utilization: float = 0.8
    utilization_spread: float = 0.1
    backlog_target_pct: float = 0.5
    include_paused_backlog: bool = False
    calendar_mode: str = "apply"
    calendar_uncertainty: float = 0.25
    custom_factors: list[dict[str, Any]] = field(default_factory=list)
    label: str = ""

    def standard_minutes(self, category: str) -> tuple[dict[str, float], bool]:
        """返回 (min/typical/max, 是否为假设值)。未知类别使用假设并标记。"""
        if category in self.service_minutes:
            return self.service_minutes[category], False
        if category in DEFAULT_SERVICE_MINUTES:
            return {k: float(v) for k, v in DEFAULT_SERVICE_MINUTES[category].items()}, False
        return dict(UNKNOWN_CATEGORY_MINUTES), True

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_week": self.start_week,
            "horizon_weeks": self.horizon_weeks,
            "history_weeks": self.history_weeks,
            "late_entry_mode": self.late_entry_mode,
            "weekly_trend_pct": round(self.weekly_trend_pct, 4),
            "service_minutes": {
                category: {key: round(float(value), 1) for key, value in values.items()}
                for category, values in self.service_minutes.items()
            },
            "utilization": round(self.utilization, 4),
            "utilization_spread": round(self.utilization_spread, 4),
            "backlog_target_pct": round(self.backlog_target_pct, 4),
            "include_paused_backlog": self.include_paused_backlog,
            "calendar_mode": self.calendar_mode,
            "calendar_uncertainty": round(self.calendar_uncertainty, 4),
            "custom_factors": [
                {
                    "grid": factor["grid"],
                    "week": factor["week"],
                    "factor": round(float(factor["factor"]), 4),
                    "label": factor.get("label", ""),
                }
                for factor in self.custom_factors
            ],
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "Scenario":
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ScenarioError("情景参数应为对象")

        start_week = _text(payload.get("start_week", ""), "start_week", allow_empty=True)
        if start_week:
            try:
                parse_week(start_week)
            except WeekFormatError as exc:
                raise ScenarioError(str(exc)) from exc

        horizon = _int(payload.get("horizon_weeks", 2), "horizon_weeks")
        if not 1 <= horizon <= MAX_HORIZON:
            raise ScenarioError(f"horizon_weeks 应在 1–{MAX_HORIZON} 之间")
        history = _int(payload.get("history_weeks", 4), "history_weeks")
        if not 1 <= history <= MAX_HISTORY:
            raise ScenarioError(f"history_weeks 应在 1–{MAX_HISTORY} 之间")

        late_mode = _text(payload.get("late_entry_mode", "range"), "late_entry_mode")
        if late_mode not in LATE_ENTRY_MODES:
            raise ScenarioError("late_entry_mode 只能是 range、include 或 exclude")

        trend = _number(payload.get("weekly_trend_pct", 0.0), "weekly_trend_pct")
        if not -0.2 <= trend <= 0.2:
            raise ScenarioError("weekly_trend_pct 应在 -20% 到 20% 之间")

        utilization = _number(payload.get("utilization", 0.8), "utilization")
        if not 0.1 <= utilization <= 1.0:
            raise ScenarioError("utilization 应在 0.1 到 1.0 之间")
        spread = _number(payload.get("utilization_spread", 0.1), "utilization_spread")
        if not 0.0 <= spread <= 0.4:
            raise ScenarioError("utilization_spread 应在 0 到 0.4 之间")
        if utilization - spread < 0:
            raise ScenarioError("utilization 减 utilization_spread 不能小于 0")

        target = _number(payload.get("backlog_target_pct", 0.5), "backlog_target_pct")
        if not 0.0 <= target <= 1.0:
            raise ScenarioError("backlog_target_pct 应在 0 到 1 之间")

        calendar_mode = _text(payload.get("calendar_mode", "apply"), "calendar_mode")
        if calendar_mode not in CALENDAR_MODES:
            raise ScenarioError("calendar_mode 只能是 apply 或 ignore")
        calendar_uncertainty = _number(payload.get("calendar_uncertainty", 0.25), "calendar_uncertainty")
        if not 0.0 <= calendar_uncertainty <= 1.0:
            raise ScenarioError("calendar_uncertainty 应在 0 到 1 之间")

        service_minutes: dict[str, dict[str, float]] = {}
        raw_standards = payload.get("service_minutes", {})
        if not isinstance(raw_standards, dict):
            raise ScenarioError("service_minutes 应为对象")
        for category, values in raw_standards.items():
            category = _text(category, "service_minutes 的类别")
            if not isinstance(values, dict):
                raise ScenarioError(f"service_minutes.{category} 应为对象")
            low = _number(values.get("low"), f"service_minutes.{category}.low")
            typical = _number(values.get("typical"), f"service_minutes.{category}.typical")
            high = _number(values.get("high"), f"service_minutes.{category}.high")
            if not 1 <= low <= typical <= high <= 24 * 60:
                raise ScenarioError(f"{category} 的工时应满足 1 ≤ low ≤ typical ≤ high ≤ 1440 分钟")
            service_minutes[category] = {"low": low, "typical": typical, "high": high}

        custom_factors: list[dict[str, Any]] = []
        raw_factors = payload.get("custom_factors", [])
        if not isinstance(raw_factors, list):
            raise ScenarioError("custom_factors 应为数组")
        for index, item in enumerate(raw_factors):
            if not isinstance(item, dict):
                raise ScenarioError(f"custom_factors[{index}] 应为对象")
            week = _text(item.get("week"), f"custom_factors[{index}].week")
            try:
                parse_week(week)
            except WeekFormatError as exc:
                raise ScenarioError(str(exc)) from exc
            grid = _text(item.get("grid"), f"custom_factors[{index}].grid")
            factor = _number(item.get("factor"), f"custom_factors[{index}].factor")
            if not 0.1 <= factor <= 5.0:
                raise ScenarioError(f"custom_factors[{index}].factor 应在 0.1 到 5 之间")
            label = _text(item.get("label", ""), f"custom_factors[{index}].label", allow_empty=True)
            custom_factors.append({"grid": grid, "week": week, "factor": factor, "label": label})

        return cls(
            start_week=start_week,
            horizon_weeks=horizon,
            history_weeks=history,
            late_entry_mode=late_mode,
            weekly_trend_pct=trend,
            service_minutes=service_minutes,
            utilization=utilization,
            utilization_spread=spread,
            backlog_target_pct=target,
            include_paused_backlog=bool(payload.get("include_paused_backlog", False)),
            calendar_mode=calendar_mode,
            calendar_uncertainty=calendar_uncertainty,
            custom_factors=custom_factors,
            label=_text(payload.get("label", ""), "label", allow_empty=True),
        )

    def resolved_start_week(self, latest_arrival_week: str | None, first_shift_week: str | None) -> str:
        if self.start_week:
            return self.start_week
        if latest_arrival_week:
            return shift_week(latest_arrival_week, 1)
        if first_shift_week:
            return first_shift_week
        raise ScenarioError("没有可用于推断起始周的历史资料，请指定 start_week")

    def effective_utilization(self) -> tuple[float, float, float]:
        """返回 (低, 点估计, 高) 的可兑现率。"""
        low = _clamp(self.utilization - self.utilization_spread, 0.0, 1.0)
        high = _clamp(self.utilization + self.utilization_spread, 0.0, 1.0)
        return low, self.utilization, high

    def custom_multiplier(self, week: str, grid: str) -> dict[str, Any]:
        """返回该周/网格的自定义情景乘数及区间。"""
        events: list[dict[str, Any]] = []
        low = typical = high = 1.0
        for item in self.custom_factors:
            if item["week"] == week and item["grid"] == grid:
                factor = float(item["factor"])
                spread = self.calendar_uncertainty
                f_low = 1 + (factor - 1) * (1 - spread)
                f_high = 1 + (factor - 1) * (1 + spread)
                low *= f_low
                typical *= factor
                high *= f_high
                events.append(
                    {
                        "source": "scenario",
                        "event": item.get("label") or "情景乘数",
                        "factor": factor,
                        "factor_low": round(f_low, 4),
                        "factor_high": round(f_high, 4),
                    }
                )
        return {"low": low, "typical": typical, "high": high, "events": events}

    def trend_factor(self, week_index: int) -> float:
        return (1.0 + self.weekly_trend_pct) ** week_index

    def future_weeks(self, start: str) -> list[str]:
        return [shift_week(start, index) for index in range(self.horizon_weeks)]

    def describe(self) -> list[dict[str, str]]:
        low_u, point_u, high_u = self.effective_utilization()
        policies = {
            "range": "录入延迟按区间处理：低值剔除 entered_late，高值保留全部记录。",
            "include": "录入延迟全部保留在到件量中（可能夸大当期负荷）。",
            "exclude": "录入延迟全部剔除（若迟到件属于更早周，可能低估总量）。",
        }
        trend_text = (
            "不外推趋势，未来周沿用历史加权均值。"
            if abs(self.weekly_trend_pct) < 1e-9
            else f"从第二周起每周按 {self.weekly_trend_pct * 100:+.1f}% 外推，属情景假设。"
        )
        return [
            {
                "topic": "服务工时",
                "detail": "按事项类别使用分钟/件；资料未提供标准工时，默认值是可调整的显式假设。",
                "source": "assumption",
            },
            {
                "topic": "需求基线",
                "detail": f"取起始周之前最近 {self.history_weeks} 个有资料的周，按近因加权平均，并区分缺失与零记录。",
                "source": "derived",
            },
            {
                "topic": "录入延迟",
                "detail": policies[self.late_entry_mode],
                "source": "derived",
            },
            {
                "topic": "趋势与季节",
                "detail": trend_text + " 季节效应没有历史因果估计，需用日历事件或情景乘数显式表达。",
                "source": "assumption",
            },
            {
                "topic": "可兑现率",
                "detail": f"班次扣除休息与重叠后，按 {point_u * 100:.0f}% 可兑现率折算（区间 {low_u * 100:.0f}%–{high_u * 100:.0f}%），其余视为行政、移动等非直接服务时间。",
                "source": "assumption",
            },
            {
                "topic": "未结积压",
                "detail": "未结事项按剩余工时独立列为积压，不并入新到件需求；"
                + ("挂起事项也纳入消化目标，" if self.include_paused_backlog else "挂起事项默认不纳入消化目标，")
                + f"每周按可纳入积压的 {self.backlog_target_pct * 100:.0f}% 安排消化，且只在新需求之后使用剩余能力。",
                "source": "policy",
            },
            {
                "topic": "日历系数",
                "detail": (
                    "应用 calendar.factor，并按其偏离 1 的幅度附加 ±"
                    f"{self.calendar_uncertainty * 100:.0f}% 不确定性；该系数是合成情景输入，不是已验证的因果系数。"
                    if self.calendar_mode == "apply"
                    else "不应用资料中的日历系数；仅使用显式情景乘数。"
                ),
                "source": "assumption",
            },
            {
                "topic": "技能不互通",
                "detail": "能力按网格与技能分别核算；某个技能无人可用时直接列为未满足需求，其他技能的空闲时数不得抵扣。",
                "source": "policy",
            },
            {
                "topic": "资料缺口",
                "detail": "没有到件记录的网格/类别标记为不可预测；只有零记录时标记低置信度，均不默认当作零需求。",
                "source": "policy",
            },
        ]
