"""Curation contract tests using measured-data fixtures; no simulator required."""
from copy import deepcopy
import math
import unittest

from analog_design.metrics import score
from benchmark import candidates, select
from benchmark.baselines import METHODS


def metrics(**changes):
    values = {
        "gain_db": 90.0, "unity_gain_hz": 4e6, "phase_margin_deg": 80.0,
        "power_w": 1e-4, "dc_error_v": 1e-4, "max_tracking_error_v": 2e-4,
        "settling_rise_s": 1e-7, "settling_fall_s": 1.5e-7,
    }
    return {**values, **changes}


def domain():
    public = {"W": 1.0, "IBIAS": 2.9999999999999997e-5}
    task = {
        "id": "domain_fixture", "circuit_directory": "circuits/fixture",
        "subcircuit": "fixture", "conditions": {"supply_v": 1.8},
        "ac": {}, "transient": {}, "max_evaluations": 30,
        "parameters": {
            "W": {"min": 1, "max": 100, "unit": "um"},
            "IBIAS": {"min": 1e-5, "max": 1e-3, "unit": "A"},
        },
        "initial_parameters": deepcopy(public),
        "constraints": {
            "gain_db": {"min": 50}, "unity_gain_hz": {"min": 1e5},
            "phase_margin_deg": {"min": 60}, "power_w": {"max": 2e-3},
            "dc_error_v": {"max": 0.02}, "max_tracking_error_v": {"max": 0.02},
            "settling_rise_s": {"max": 1e-5}, "settling_fall_s": {"max": 1e-5},
        },
    }
    return {"name": "fixture", "family": "fixture_family", "task": task,
            "public_default": public}


def design(label, width, bias, measured):
    return {"status": "ok", "parameters": {"W": width, "IBIAS": bias},
            "metrics": measured, "measurement_directory": "runs/fixture/" + label}


def method_panel(trials=20, rate=0.0, median=30):
    return {
        method: {"completed_trials": trials,
                 "success_at": {"30": {"rate": rate}},
                 "median_calls_censored_at_30": median}
        for method in METHODS
    }


def observation(name, reference, *, topology="fixture", level="medium",
                signature=None, group=None):
    task = {"parameters": {"W": {"min": 1, "max": 100, "unit": "um"}}}
    candidate = {
        "id": name, "topology": topology, "task": task, "reference": {"W": reference},
        "requirement_group": group or "group_" + name,
        "empirical_solution_signature": signature or "signature_" + name,
        "empirical_pool_feasible_count": 5,
    }
    return {"candidate": candidate, "observation": {"methods": method_panel()},
            "level": level, "calibration_directory": "runs/calibration_fixture"}


class CandidateConstructionTests(unittest.TestCase):
    def setUp(self):
        self.entry = domain()
        self.default = design("default", 1.0, 3e-5, metrics(gain_db=45))
        self.reference = design("reference", 4.0, 5e-5, metrics())

    def test_rounded_public_default_matches_without_silently_disabling_filter(self):
        self.assertNotEqual(self.default["parameters"], self.entry["public_default"])
        generated = candidates.create_candidates(self.entry, [self.default, self.reference])
        self.assertGreater(len(generated), 0)
        for candidate in generated:
            self.assertFalse(score(self.default["metrics"], candidate["task"]["constraints"])["success"])
            self.assertTrue(candidates.reference_margin(self.reference["metrics"], candidate["task"]["constraints"]))
            self.assertEqual(candidate["calibration_status"], "pending")
            self.assertFalse(candidate["training_approved"])

    def test_missing_public_default_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Missing measured public default"):
            candidates.create_candidates(self.entry, [self.reference])

    def test_passing_public_default_rejects_candidates_even_with_failing_start_available(self):
        strongest = metrics(gain_db=120, unity_gain_hz=8e6, phase_margin_deg=95,
                            power_w=1e-5, dc_error_v=1e-5, max_tracking_error_v=1e-5,
                            settling_rise_s=1e-8, settling_fall_s=1e-8)
        public = design("default", 1.0, 3e-5, strongest)
        failing = design("failing", 2.0, 4e-5, metrics(gain_db=45))
        generated = candidates.create_candidates(self.entry, [public, self.reference, failing])
        self.assertEqual(generated, [])

    def test_boundary_success_does_not_count_as_robust_reference(self):
        for name, constraints in (
            ("gain_db", {"gain_db": {"min": 60}}),
            ("phase_margin_deg", {"phase_margin_deg": {"min": 60}}),
            ("power_w", {"power_w": {"max": 1e-3}}),
            ("settling_rise_s", {"settling_rise_s": {"max": 1e-6}}),
        ):
            with self.subTest(metric=name):
                target = next(iter(constraints[name].values()))
                measured = {name: target}
                self.assertTrue(score(measured, constraints)["success"])
                self.assertFalse(candidates.reference_margin(measured, constraints))

    def test_reference_requires_absolute_and_relative_slack(self):
        self.assertFalse(candidates.reference_margin({"phase_margin_deg": 60.299}, {"phase_margin_deg": {"min": 60}}))
        self.assertTrue(candidates.reference_margin({"phase_margin_deg": 60.301}, {"phase_margin_deg": {"min": 60}}))
        self.assertFalse(candidates.reference_margin({"power_w": 0.000981}, {"power_w": {"max": 0.001}}))
        self.assertTrue(candidates.reference_margin({"power_w": 0.00097}, {"power_w": {"max": 0.001}}))

    def test_nonfinite_measurements_cannot_have_reference_margin(self):
        for value in (math.nan, math.inf, -math.inf):
            for direction in ("min", "max"):
                with self.subTest(value=value, direction=direction):
                    self.assertFalse(candidates.reference_margin({"gain_db": value}, {"gain_db": {direction: 60}}))

    def test_target_grids_preserve_physical_quality_floors_and_reference_feasibility(self):
        baseline = self.entry["task"]["constraints"]
        for level in ("easy", "medium", "hard"):
            for profile in ("balanced", "speed_power", "gain_accuracy", "stability_settling"):
                with self.subTest(level=level, profile=profile):
                    proposed = candidates.propose_constraints(self.entry["task"], metrics(), level, profile)
                    self.assertEqual(set(proposed), set(candidates.METRICS))
                    for name, limits in proposed.items():
                        direction, target = next(iter(limits.items()))
                        floor = baseline[name][direction]
                        self.assertTrue(target >= floor if direction == "min" else target <= floor)
                        self.assertIn(target, [floor, *candidates.GRIDS[name]])
                    self.assertTrue(candidates.reference_margin(metrics(), proposed))

    def test_requirement_dedup_and_feasible_set_signature_are_order_independent(self):
        generated = candidates.create_candidates(self.entry, [self.default, self.reference])
        reordered = candidates.create_candidates(self.entry, [self.reference, self.default])
        groups = [row["requirement_group"] for row in generated]
        self.assertEqual(len(groups), len(set(groups)))
        signatures = {row["requirement_group"]: row["empirical_solution_signature"] for row in generated}
        self.assertEqual(signatures, {row["requirement_group"]: row["empirical_solution_signature"] for row in reordered})


class DifficultyEvidenceTests(unittest.TestCase):
    def test_full_method_panel_is_required_for_calibrated_levels(self):
        for missing in METHODS:
            incomplete = method_panel()
            del incomplete[missing]
            with self.subTest(missing=missing):
                self.assertEqual(select.observed_level(incomplete), "incomplete")

    def test_screening_failures_are_not_twenty_trial_hard_evidence(self):
        self.assertEqual(select.observed_level(method_panel(trials=3), minimum_trials=3), "hard_candidate")
        self.assertEqual(select.observed_level(method_panel(trials=10)), "hard_candidate")
        self.assertEqual(select.observed_level(method_panel(trials=19)), "hard_candidate")
        self.assertEqual(select.observed_level(method_panel(trials=20)), "hard")

    def test_hard_trial_override_cannot_lower_twenty_trial_evidence_floor(self):
        self.assertNotEqual(select.observed_level(method_panel(trials=3), minimum_trials=3, hard_trials=3), "hard")

    def test_each_method_needs_enough_trials_before_calibrated_hard(self):
        methods = method_panel(trials=20)
        methods[METHODS[-1]]["completed_trials"] = 19
        self.assertEqual(select.observed_level(methods), "hard_candidate")
        methods[METHODS[-1]]["completed_trials"] = 9
        self.assertEqual(select.observed_level(methods), "incomplete")

    def test_easy_requires_both_success_frequency_and_fast_censored_median(self):
        self.assertEqual(select.observed_level(method_panel(trials=10, rate=0.8, median=10)), "easy")
        self.assertEqual(select.observed_level(method_panel(trials=10, rate=0.8, median=11)), "medium")
        self.assertEqual(select.observed_level(method_panel(trials=10, rate=0.7, median=5)), "medium")

    def test_one_effective_method_prevents_hard_label(self):
        methods = method_panel()
        methods[METHODS[-1]]["success_at"]["30"]["rate"] = 0.25
        self.assertEqual(select.observed_level(methods), "medium")


class CurriculumSelectionTests(unittest.TestCase):
    def choose_pair(self, first, second):
        return select.choose([first, second], {"easy": 0, "medium": 2, "hard": 0})

    def test_exact_reference_is_not_reused_for_different_targets(self):
        chosen, report = self.choose_pair(observation("a", 2), observation("b", 2))
        self.assertEqual(len(chosen), 1)
        self.assertEqual(report["rejected_by_selection"]["duplicate_reference"], 1)
        self.assertFalse(report["complete"])

    def test_distinct_but_nearby_references_do_not_inflate_diversity(self):
        chosen, report = self.choose_pair(observation("a", 2), observation("b", 2.1))
        self.assertEqual(len(chosen), 1)
        self.assertEqual(report["rejected_by_selection"]["reference_too_close"], 1)

    def test_distinct_references_with_identical_empirical_feasible_set_are_deduplicated(self):
        chosen, report = self.choose_pair(observation("a", 2, signature="same"),
                                          observation("b", 8, signature="same"))
        self.assertEqual(len(chosen), 1)
        self.assertEqual(report["rejected_by_selection"]["same_characterization_solution_set"], 1)

    def test_requirement_group_dedup_is_independent_of_reference_diversity(self):
        chosen, report = self.choose_pair(observation("a", 2, group="same"),
                                          observation("b", 8, group="same"))
        self.assertEqual(len(chosen), 1)
        self.assertEqual(report["rejected_by_selection"]["duplicate_requirement_group"], 1)

    def test_references_and_solution_signatures_are_scoped_to_topology(self):
        chosen, report = self.choose_pair(observation("a", 2, topology="one", signature="same"),
                                          observation("b", 2, topology="two", signature="same"))
        self.assertEqual(len(chosen), 2)
        self.assertTrue(report["complete"])

    def test_diversity_restrictions_and_topology_cap_report_shortfall_without_padding(self):
        rows = [observation("a", 2), observation("b", 8), observation("c", 32)]
        chosen, report = select.choose(rows, {"easy": 2, "medium": 3, "hard": 1}, per_topology_limit=1)
        self.assertEqual(len(chosen), 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["selected_levels"].get("medium"), 1)
        self.assertEqual(report["selected_topologies"], {"fixture": 1})
        self.assertEqual(report["rejected_by_selection"]["topology_cap"], 2)

    def test_screening_hard_candidates_are_not_selected_by_default(self):
        chosen, report = select.choose([observation("a", 2, level="hard_candidate")],
                                         {"easy": 0, "medium": 0, "hard": 1})
        self.assertEqual(chosen, [])
        self.assertFalse(report["complete"])

    def test_explicit_screening_selection_preserves_observed_hard_candidate_label(self):
        chosen, report = select.choose([observation("a", 2, level="hard_candidate")],
                                         {"easy": 0, "medium": 0, "hard": 1}, allow_hard_candidates=True)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["level"], "hard_candidate")


if __name__ == "__main__":
    unittest.main()
