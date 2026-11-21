import unittest
from pathlib import Path

from app.analytics import (
    INSUFFICIENT,
    UNFILLABLE,
    UNPREDICTABLE,
    analyse,
    apply_adjustments,
    diff_plan,
)
from app.config import Scenario, ScenarioError
from app.data import DataError, load_dataset

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def scenario(**overrides):
    payload = {"start_week": "2026-W20", "horizon_weeks": 2}
    payload.update(overrides)
    return Scenario.from_dict(payload)


def run(**overrides):
    return analyse(load_dataset(FIXTURES), scenario(**overrides))


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset(FIXTURES)

    def test_cancel_reduces_history_without_removing_original(self):
        cells, log = apply_adjustments(
            self.dataset,
            "range",
            [
                {
                    "id": "A1",
                    "kind": "cancel",
                    "grid": "G2",
                    "week": "2026-W19",
                    "category": "repair",
                    "count_delta": -5,
                }
            ],
        )
        cell = next(c for c in cells if (c.grid, c.week) == ("G2", "2026-W19"))
        self.assertEqual(cell.raw_count, 10)
        self.assertEqual(cell.adjustments[0]["id"], "A1")
        self.assertEqual(log[0]["origin"], "runtime")
        # 原始记录仍在资料里，可追溯
        self.assertEqual(
            [a.count for a in self.dataset.arrivals if a.id == "D-4"], [15]
        )

    def test_transfer_moves_count_between_grids(self):
        cells, _ = apply_adjustments(
            self.dataset,
            "range",
            [
                {
                    "id": "T1",
                    "kind": "transfer",
                    "grid": "G1",
                    "week": "2026-W19",
                    "category": "visit",
                    "count_delta": -4,
                    "to_grid": "G2",
                }
            ],
        )
        by_key = {(c.grid, c.week, c.category): c for c in cells}
        self.assertEqual(by_key[("G1", "2026-W19", "visit")].raw_count, 15)
        self.assertEqual(by_key[("G2", "2026-W19", "visit")].raw_count, 4)
        self.assertEqual(by_key[("G2", "2026-W19", "visit")].entered_late, 0)

    def test_supplement_raises_baseline(self):
        base = run()["summary"]["demand_hours"]["typical"]
        supplemented = analyse(
            self.dataset,
            scenario(),
            [
                {
                    "id": "S1",
                    "kind": "supplement",
                    "grid": "G1",
                    "week": "2026-W19",
                    "category": "visit",
                    "count_delta": 6,
                }
            ],
        )["summary"]["demand_hours"]["typical"]
        self.assertGreater(supplemented, base)

    def test_over_cancel_is_rejected(self):
        with self.assertRaises(DataError):
            apply_adjustments(
                self.dataset,
                "range",
                [
                    {
                        "id": "X1",
                        "kind": "cancel",
                        "grid": "G2",
                        "week": "2026-W19",
                        "category": "repair",
                        "count_delta": -999,
                    }
                ],
            )

    def test_transfer_requires_target(self):
        with self.assertRaises(DataError):
            apply_adjustments(
                self.dataset,
                "range",
                [
                    {
                        "id": "X2",
                        "kind": "transfer",
                        "grid": "G2",
                        "week": "2026-W19",
                        "category": "repair",
                        "count_delta": -1,
                    }
                ],
            )


class LateEntryTests(unittest.TestCase):
    def test_late_entry_modes_bound_effective_counts(self):
        include = run(late_entry_mode="include")
        exclude = run(late_entry_mode="exclude")
        ranged = run(late_entry_mode="range")
        self.assertGreater(
            include["summary"]["demand_hours"]["typical"],
            exclude["summary"]["demand_hours"]["typical"],
        )
        self.assertLessEqual(ranged["summary"]["demand_hours"]["low"], ranged["summary"]["demand_hours"]["typical"])
        self.assertLessEqual(ranged["summary"]["demand_hours"]["typical"], ranged["summary"]["demand_hours"]["high"])

    def test_range_keeps_low_and_high_per_cell(self):
        result = run()
        cell = next(
            c for c in result["history"]["cells"] if (c["grid"], c["week"]) == ("G1", "2026-W19")
        )
        self.assertEqual(cell["count_low"], 14)
        self.assertEqual(cell["count_high"], 19)


class BacklogTests(unittest.TestCase):
    def test_backlog_is_separate_from_new_demand(self):
        result = run()
        demand = result["summary"]["demand_hours"]["typical"]
        backlog_hours = sum(item["estimated_hours"] for item in result["backlog"]["items"])
        self.assertNotAlmostEqual(demand, demand + backlog_hours)
        self.assertEqual(result["summary"]["backlog_hours"], 20.0)

    def test_paused_excluded_by_default_and_included_on_request(self):
        default = run()
        paused = next(item for item in default["backlog"]["items"] if item["case"] == "K-2")
        self.assertFalse(paused["eligible_for_plan"])
        included = run(include_paused_backlog=True)
        paused_included = next(item for item in included["backlog"]["items"] if item["case"] == "K-2")
        self.assertTrue(paused_included["eligible_for_plan"])
        self.assertGreater(
            included["summary"]["backlog_eligible_hours"],
            default["summary"]["backlog_eligible_hours"],
        )

    def test_backlog_target_uses_capacity_left_after_new_demand(self):
        for target in run()["backlog"]["targets"]:
            self.assertGreaterEqual(target["capacity_after_new_demand_hours"], 0)
            self.assertLessEqual(
                target["coverable_hours"], target["capacity_after_new_demand_hours"] + 1e-9
            )


class CapacityTests(unittest.TestCase):
    def test_overlapping_cross_grid_shift_counted_once(self):
        result = run()
        # W-1：G1 09:00–17:00 减 60 分钟休息 = 7h；G2 13:00–16:00 与 G1 重叠，计 0。
        g1 = next(
            c for c in result["capacity"]
            if (c["week"], c["grid"], c["skill"]) == ("2026-W20", "G1", "visit")
        )
        g2 = next(
            c for c in result["capacity"]
            if (c["week"], c["grid"], c["skill"]) == ("2026-W20", "G2", "visit")
        )
        self.assertEqual(g1["allocated_hours"], 7.0)
        self.assertEqual(g2["allocated_hours"], 0.0)
        self.assertTrue(result["conflicts"])

    def test_break_minutes_are_deducted(self):
        result = run()
        # W-3：10:00–14:00，无休息 = 4h（即使需求不可预测，班次上限仍应按休息扣减）
        g1_repair = next(
            c for c in result["capacity"]
            if (c["week"], c["grid"], c["skill"]) == ("2026-W20", "G1", "repair")
        )
        self.assertEqual(g1_repair["gross_hours"], 4.0)
        # W-2：09:00–15:00 减 30 分钟休息 = 5.5h
        g2_repair = next(
            c for c in result["capacity"]
            if (c["week"], c["grid"], c["skill"]) == ("2026-W20", "G2", "repair")
        )
        self.assertEqual(g2_repair["allocated_hours"], 5.5)

    def test_expired_skill_is_not_counted(self):
        result = run()
        support = [
            c for c in result["capacity"]
            if c["skill"] == "support" and c["week"] == "2026-W20"
        ]
        self.assertTrue(all(c["allocated_hours"] == 0 for c in support))

    def test_unfillable_skill_is_not_covered_by_other_skills(self):
        result = run()
        g3 = next(
            item for item in result["coverage"]
            if (item["week"], item["grid"], item["category"]) == ("2026-W20", "G3", "support")
        )
        self.assertEqual(g3["status"], UNFILLABLE)
        self.assertEqual(g3["gap_hours"]["high"], 1.0)
        # 其他技能即使有空闲，也不能抵消
        unmet = result["summary"]["unfillable_demand_hours"]["typical"]
        self.assertGreater(unmet, 0)

    def test_skills_are_not_summed_across_categories(self):
        result = run()
        for cell in result["capacity"]:
            for person in cell["people"]:
                self.assertLessEqual(person["hours"], 7.0 + 1e-9)


class PredictionUncertaintyTests(unittest.TestCase):
    def test_missing_history_is_not_zero_demand(self):
        result = run()
        g1_repair = next(
            item for item in result["coverage"]
            if (item["grid"], item["category"]) == ("G1", "repair")
        )
        self.assertEqual(g1_repair["status"], UNPREDICTABLE)
        self.assertIsNone(g1_repair["demand_hours"])
        self.assertEqual(result["summary"]["unpredictable_cells"], 12)

    def test_insufficient_history_within_window_is_distinct(self):
        result = run(history_weeks=1)
        g1_repair = next(
            f for f in result["forecast"]
            if (f["grid"], f["category"], f["week"]) == ("G1", "repair", "2026-W20")
        )
        # G1/repair 在任何窗口都没有记录 → unpredictable
        self.assertEqual(g1_repair["status"], UNPREDICTABLE)

        result = run(history_weeks=1, start_week="2026-W19")
        g1_visit = next(
            f for f in result["forecast"]
            if (f["grid"], f["category"], f["week"]) == ("G1", "visit", "2026-W19")
        )
        self.assertNotIn(g1_visit["status"], {UNPREDICTABLE, INSUFFICIENT})
        # G3/support 有记录但落在窗口外 → 资料不足，与「完全没有记录」区分
        g3_support = next(
            f for f in result["forecast"]
            if (f["grid"], f["category"], f["week"]) == ("G3", "support", "2026-W19")
        )
        self.assertEqual(g3_support["status"], INSUFFICIENT)

    def test_single_observation_is_low_confidence_and_wide(self):
        result = run(history_weeks=1)
        g1_visit = next(
            f for f in result["forecast"]
            if (f["grid"], f["category"]) == ("G1", "visit")
        )
        self.assertEqual(g1_visit["confidence"], "low")
        self.assertGreater(
            g1_visit["demand_cases"]["high"] - g1_visit["demand_cases"]["low"], 0
        )

    def test_zero_records_stay_zero_but_low_confidence(self):
        result = run()
        g3 = next(
            item for item in result["forecast"]
            if (item["grid"], item["category"]) == ("G3", "support")
        )
        self.assertEqual(g3["status"], "low_confidence")
        self.assertEqual(g3["demand_cases"]["typical"], 0)


class CalendarTests(unittest.TestCase):
    def test_calendar_factor_applied_only_to_matching_grid_week(self):
        # calendar.json：G2 在 2026-W19 有 market ×1.4
        plain = run(start_week="2026-W19", calendar_mode="ignore")
        applied = run(start_week="2026-W19", calendar_mode="apply")

        def demand(result, grid, category="repair"):
            return next(
                item["demand_hours"]["typical"]
                for item in result["forecast"]
                if (item["week"], item["grid"], item["category"]) == ("2026-W19", grid, category)
            )

        self.assertAlmostEqual(demand(applied, "G2"), demand(plain, "G2") * 1.4, places=1)
        # 其他网格不受该网格事件影响
        self.assertAlmostEqual(demand(applied, "G1", "visit"), demand(plain, "G1", "visit"), places=2)

    def test_calendar_uncertainty_widens_interval(self):
        narrow = run(start_week="2026-W19", calendar_uncertainty=0.0)
        wide = run(start_week="2026-W19", calendar_uncertainty=0.5)
        narrow_range = next(
            f for f in narrow["forecast"]
            if (f["week"], f["grid"]) == ("2026-W19", "G2") and f["category"] == "repair"
        )
        wide_range = next(
            f for f in wide["forecast"]
            if (f["week"], f["grid"]) == ("2026-W19", "G2") and f["category"] == "repair"
        )
        self.assertGreater(
            wide_range["demand_hours"]["high"] - wide_range["demand_hours"]["low"],
            narrow_range["demand_hours"]["high"] - narrow_range["demand_hours"]["low"],
        )


class ScenarioValidationTests(unittest.TestCase):
    def test_rejects_bad_inputs(self):
        for payload in (
            {"horizon_weeks": 0},
            {"horizon_weeks": 99},
            {"late_entry_mode": "maybe"},
            {"utilization": 1.5},
            {"utilization": 0.1, "utilization_spread": 0.4},
            {"weekly_trend_pct": 0.5},
            {"service_minutes": {"visit": {"low": 60, "typical": 30, "high": 90}}},
            {"custom_factors": [{"week": "2026-W99", "grid": "G1", "factor": 1.2}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(ScenarioError):
                Scenario.from_dict(payload)

    def test_default_start_week_follows_latest_arrival(self):
        scenario_obj = Scenario.from_dict({})
        dataset = load_dataset(FIXTURES)
        self.assertEqual(
            scenario_obj.resolved_start_week(dataset.latest_arrival_week(), None),
            "2026-W20",
        )


class PlanVersionTests(unittest.TestCase):
    def test_diff_reports_changes_without_touching_confirmed(self):
        confirmed = [
            {"week": "2026-W20", "grid": "G1", "skill": "visit", "person": "W-1", "hours": 7.0},
            {"week": "2026-W20", "grid": "G2", "skill": "repair", "person": "W-2", "hours": 4.0},
        ]
        recommended = [
            {"week": "2026-W20", "grid": "G1", "skill": "visit", "person": "W-1", "hours": 7.0},
            {"week": "2026-W20", "grid": "G2", "skill": "repair", "person": "W-2", "hours": 5.5},
            {"week": "2026-W20", "grid": "G1", "skill": "repair", "person": "W-3", "hours": 4.0},
        ]
        diff = diff_plan(confirmed, recommended)
        self.assertTrue(diff["has_changes"])
        self.assertEqual(len(diff["changed"]), 1)
        self.assertEqual(len(diff["added"]), 1)
        self.assertEqual(confirmed[1]["hours"], 4.0)

    def test_identical_plans_have_no_changes(self):
        plan = [{"week": "2026-W20", "grid": "G1", "skill": "visit", "person": "W-1", "hours": 7.0}]
        self.assertFalse(diff_plan(plan, list(plan))["has_changes"])


class InvariantTests(unittest.TestCase):
    def test_gap_never_negative_and_ranges_ordered(self):
        result = run()
        for item in result["coverage"]:
            if item["gap_hours"]:
                self.assertGreaterEqual(item["gap_hours"]["low"], 0)
                self.assertLessEqual(item["gap_hours"]["low"], item["gap_hours"]["typical"] + 1e-9)
                self.assertLessEqual(item["gap_hours"]["typical"], item["gap_hours"]["high"] + 1e-9)
        for forecast in result["forecast"]:
            counts = forecast["demand_cases"]
            self.assertLessEqual(counts["low"], counts["typical"] + 1e-9)
            self.assertLessEqual(counts["typical"], counts["high"] + 1e-9)

    def test_fingerprint_changes_with_inputs(self):
        base = run()["meta"]["input_fingerprint"]
        changed = run(late_entry_mode="include")["meta"]["input_fingerprint"]
        self.assertNotEqual(base, changed)

    def test_assumptions_are_exposed(self):
        topics = {item["topic"] for item in run()["meta"]["assumptions"]}
        self.assertIn("服务工时", topics)
        self.assertIn("技能不互通", topics)
        self.assertIn("资料缺口", topics)


if __name__ == "__main__":
    unittest.main()
