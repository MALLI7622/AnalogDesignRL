from copy import deepcopy
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark import start_designs as starts
from benchmark.simulation import canonical_hash


def definition():
    return {"id": "synthetic", "parameters": {
        "C1": {"min": 1e-12, "max": 16e-12, "unit": "F"},
        "C2": {"min": 1e-12, "max": 16e-12, "unit": "F"},
        "IBIAS": {"min": 1e-6, "max": 16e-6, "unit": "A"},
        "M": {"min": 1, "max": 8, "unit": "count", "integer": True}},
        "constraints": {"gain_db": {"min": 60}},
        "initial_parameters": {"C1": 4e-12, "C2": 4e-12, "IBIAS": 4e-6, "M": 3}}


class FakeCache:
    instances = []

    def __init__(self, directory, *, simulator_timeout):
        self.physical_evaluations = 0
        self.cache_hits = 0
        self.assertions = 0
        self.calls = []
        self.instances.append(self)

    def evaluate(self, task, parameters):
        self.calls.append((deepcopy(task), deepcopy(parameters)))
        self.physical_evaluations += 1
        result = {"status": "ok", "success": False,
                  "metrics": {"gain_db": 59}, "independent_metrics": {"gain_db": 59},
                  "measurement_key": canonical_hash(parameters), "measurement_directory": "runs/fake/measurement"}
        if parameters["C1"] == 8e-12:
            result.update(status="failed", metrics={"gain_db": 15}, independent_metrics={},
                          error="Native measurement did not complete.")
        elif parameters["IBIAS"] == 8e-6:
            result["independent_metrics"] = {"gain_db": 59.9}  # Fails, but not clearly in both paths.
        elif parameters["IBIAS"] == 2e-6:
            result.update(success=True, metrics={"gain_db": 61}, independent_metrics={"gain_db": 61})
        return result

    def assert_unchanged(self):
        self.assertions += 1


class StartDesignTests(unittest.TestCase):
    def test_factors_are_deterministic_and_independent_of_target_and_search(self):
        task = definition()
        reference = deepcopy(task["initial_parameters"])
        untouched = deepcopy((task, reference))
        proposals = starts.perturbations(task, reference, 0, count=8)
        expected = [{"C1": 2}, {"C1": 0.5}, {"IBIAS": 2}, {"IBIAS": 0.5},
                    {"C1": 1.5, "IBIAS": 1.5}, {"C1": 2 / 3, "IBIAS": 2 / 3},
                    {"C1": 1.5, "IBIAS": 2 / 3}, {"C1": 2 / 3, "IBIAS": 1.5}]
        self.assertEqual([row["requested_factors"] for row in proposals], expected)
        different = deepcopy(task)
        different["constraints"] = {"gain_db": {"min": 99}}
        different["baseline_trajectory"] = [{"parameters": {"C1": 1e-12}}]
        self.assertEqual(proposals, starts.perturbations(different, reference, 0, count=8))
        self.assertEqual((task, reference), untouched)
        self.assertEqual(starts.perturbations(task, reference, 1, count=1)[0]["changed_controls"], ["C2"])
        for row in proposals:
            values = row["parameters"]
            self.assertEqual(row["changed_controls"], [name for name in values if values[name] != reference[name]])
            self.assertEqual(set(row["actual_factors"]), set(row["changed_controls"]))
            for name, factor in row["actual_factors"].items():
                self.assertEqual(factor, values[name] / reference[name])
            self.assertTrue(math.isfinite(row["normalized_reference_distance"]))
            self.assertGreater(row["normalized_reference_distance"], 0)
            self.assertEqual(values["M"], reference["M"])
            for name, value in values.items():
                self.assertGreaterEqual(value, task["parameters"][name]["min"])
                self.assertLessEqual(value, task["parameters"][name]["max"])

    def test_clipped_and_duplicate_plans_do_not_claim_unchanged_controls(self):
        task = definition()
        reference = {**task["initial_parameters"], "C1": 16e-12, "IBIAS": 1e-6}
        proposals = starts.perturbations(task, reference, 0, count=8)
        self.assertEqual(len({canonical_hash(row["parameters"]) for row in proposals}), len(proposals))
        self.assertNotIn(canonical_hash(reference), {canonical_hash(row["parameters"]) for row in proposals})
        for row in proposals:
            changed = [name for name, value in row["parameters"].items() if value != reference[name]]
            self.assertEqual(row["changed_controls"], changed)
        joint = next(row for row in proposals if row["requested_factors"] == {"C1": 1.5, "IBIAS": 1.5})
        self.assertEqual(joint["changed_controls"], ["IBIAS"])
        self.assertEqual(joint["actual_factors"], {"IBIAS": 1.5})
        self.assertEqual(len(starts.perturbations(task, reference, 0, count=4)), 2)

    def test_clear_failure_is_finite_and_excludes_near_threshold_misses(self):
        self.assertEqual(starts.clear_failures({"phase_margin_deg": 60}, {"phase_margin_deg": {"min": 60}}), [])
        self.assertEqual(starts.clear_failures({"phase_margin_deg": 59.700001}, {"phase_margin_deg": {"min": 60}}), [])
        self.assertEqual(starts.clear_failures({"phase_margin_deg": 59.699999}, {"phase_margin_deg": {"min": 60}}), ["phase_margin_deg"])
        self.assertEqual(starts.clear_failures({"unity_gain_hz": 980000}, {"unity_gain_hz": {"min": 1000000}}), ["unity_gain_hz"])
        self.assertEqual(starts.clear_failures({"power_w": 1.019999e-4}, {"power_w": {"max": 1e-4}}), [])
        self.assertEqual(starts.clear_failures({"power_w": 1.020001e-4}, {"power_w": {"max": 1e-4}}), ["power_w"])
        self.assertEqual(starts.clear_failures({"settling_rise_s": 111.9e-9}, {"settling_rise_s": {"max": 100e-9}}), [])
        self.assertEqual(starts.clear_failures({"settling_rise_s": 112.1e-9}, {"settling_rise_s": {"max": 100e-9}}), ["settling_rise_s"])
        for value in (None, True, "59", float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                starts.clear_failures({"gain_db": value}, {"gain_db": {"min": 60}})
        for constraints in ({"gain_db": {"min": 0}}, {"gain_db": {"min": True}},
                            {"gain_db": {"min": float("inf")}}, {"gain_db": {"at_least": 60}},
                            {"gain_db": {"min": 50, "max": 60}}, {"unsupported": {"max": 1}}):
            with self.subTest(constraints=constraints), self.assertRaises(ValueError):
                starts.clear_failures({"gain_db": 59}, constraints)

    def test_small_characterization_keeps_invalid_records_and_planned_indices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "benchmark").mkdir()
            task = definition()
            (root / "benchmark/domains.json").write_text(json.dumps({"entries": [{"name": "ota", "task": task}]}))
            targets = root / "targets.json"
            targets.write_text(json.dumps({"targets": [{"topology": "ota", "key": "tight",
                                                        "parameters": task["initial_parameters"], "constraints": task["constraints"]}]}))
            FakeCache.instances = []
            with patch.object(starts, "ROOT", root), patch.object(starts, "CODE_FILES", ()), \
                    patch.object(starts, "MeasurementCache", FakeCache), \
                    patch.object(starts, "as_completed", lambda pending: reversed(list(pending))), \
                    contextlib.redirect_stdout(io.StringIO()):
                starts.characterize(targets, root / "output", root / "cache", workers=2)
            records = json.loads((root / "output/designs/ota.json").read_text())
            self.assertEqual(len(records), 4)
            invalid = next(row for row in records if row["status"] == "failed")
            self.assertEqual(invalid["sample_index"], 0)
            self.assertEqual(invalid["metrics"], {"gain_db": 15})
            self.assertEqual(invalid["error"], "Native measurement did not complete.")
            self.assertFalse(invalid["eligible_failing_start"])
            self.assertIsNone(invalid["target_score"])
            self.assertTrue(invalid["measurement_key"])
            self.assertEqual(sum(row["eligible_failing_start"] for row in records), 1)
            eligible = next(row for row in records if row["eligible_failing_start"])
            self.assertEqual(eligible["sample_index"], 1)
            self.assertEqual(eligible["clear_failure_metrics"], ["gain_db"])
            completion = json.loads((root / "output/completion.json").read_text())
            self.assertEqual(completion["measurements"], 4)
            self.assertEqual(completion["difficulty_status"], "not_calibrated")
            self.assertFalse(completion["training_approved"])
            cache = FakeCache.instances[0]
            self.assertEqual(cache.assertions, 1)
            self.assertEqual(len(cache.calls), 4)
            self.assertTrue(all(call[0]["constraints"] == task["constraints"] for call in cache.calls))

    def test_invalid_limits_rejected_before_creating_output_or_loading_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for kwargs in ({"maximum_per_topology": 0}, {"maximum_per_topology": -1},
                           {"maximum_per_topology": True}, {"per_reference": 0}, {"per_reference": 9},
                           {"workers": 0}, {"workers": True}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    starts.characterize(root / "missing.json", root / "output", root / "cache", **kwargs)
                self.assertFalse((root / "output").exists())

    def test_input_change_during_planning_aborts_before_simulation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "benchmark").mkdir()
            task = definition()
            (root / "benchmark/domains.json").write_text(json.dumps({"entries": [{"name": "ota", "task": task}]}))
            targets = root / "targets.json"
            targets.write_text(json.dumps([{"topology": "ota", "key": "tight", "parameters": task["initial_parameters"],
                                            "constraints": task["constraints"]}]))
            original = starts.perturbations

            def mutate(*args, **kwargs):
                targets.write_text("[]")
                return original(*args, **kwargs)

            with patch.object(starts, "ROOT", root), patch.object(starts, "CODE_FILES", ()), \
                    patch.object(starts, "perturbations", side_effect=mutate), \
                    patch.object(starts, "MeasurementCache") as cache, self.assertRaisesRegex(RuntimeError, "input changed"):
                starts.characterize(targets, root / "output", root / "cache", workers=1)
            cache.assert_not_called()
            self.assertFalse((root / "output/completion.json").exists())


if __name__ == "__main__":
    unittest.main()
