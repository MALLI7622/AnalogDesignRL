from copy import deepcopy
import io
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from analog_design.metrics import score
from benchmark import calibrate
from benchmark.simulation import canonical_hash, save_json


def result(gain, *, success, status="ok"):
    return {"status": status, "success": success,
            "metrics": {"gain_db": gain}, "independent_metrics": {"gain_db": gain}}


class CalibrationTests(unittest.TestCase):
    def test_wilson_handles_zero_and_small_sample_uncertainty(self):
        self.assertIsNone(calibrate.wilson_interval(0, 0))
        zero = calibrate.wilson_interval(0, 3)
        self.assertAlmostEqual(zero["low"], 0)
        self.assertGreater(zero["high"], 0.5)
        full = calibrate.wilson_interval(3, 3)
        self.assertLess(full["low"], 0.5)
        self.assertAlmostEqual(full["high"], 1)
        with self.assertRaises(ValueError):
            calibrate.wilson_interval(4, 3)

    def test_summary_thresholds_and_failure_censoring_use_logical_budget(self):
        trials = [{"success": True, "first_success_evaluation": 5, "evaluations_used": 5},
                  {"success": True, "first_success_evaluation": 8, "evaluations_used": 8},
                  {"success": True, "first_success_evaluation": 30, "evaluations_used": 30},
                  {"success": False, "first_success_evaluation": None, "evaluations_used": 2}]
        summary = calibrate.summarize_trials(trials)
        self.assertEqual(summary["success_at"]["5"]["successes"], 1)
        self.assertEqual(summary["success_at"]["10"]["successes"], 2)
        self.assertEqual(summary["success_at"]["30"]["successes"], 3)
        self.assertEqual(summary["median_calls_successes"], 8)
        self.assertEqual(summary["median_calls_censored_at_30"], 19)
        self.assertEqual(summary["logical_evaluations"], 45)
        self.assertIsNone(calibrate.summarize_trials([])["median_calls_successes"])

    def test_grouped_summary_matches_filtered_trial_reference(self):
        methods = ["uniform_log", "coordinate_search", "uniform_linear"]
        records = [{"candidate": {"id": task_id, "topology": "topology_" + task_id,
                                   "topology_family": "family", "proposed_difficulty": "medium",
                                   "profile": "balanced"}} for task_id in ("a", "b", "c")]
        admissions = {"a": {"admitted": True, "rejection_reasons": []},
                      "b": {"admitted": True, "rejection_reasons": []},
                      "c": {"admitted": False, "rejection_reasons": ["seed passes"]}}
        rows = [{"task_id": "b", "method": "uniform_log", "success": False,
                 "first_success_evaluation": None, "evaluations_used": 30},
                {"task_id": "a", "method": "coordinate_search", "success": True,
                 "first_success_evaluation": 5, "evaluations_used": 5},
                {"task_id": "a", "method": "uniform_log", "success": True,
                 "first_success_evaluation": 12, "evaluations_used": 12},
                {"task_id": "b", "method": "coordinate_search", "success": True,
                 "first_success_evaluation": 30, "evaluations_used": 30}]
        original_rows = deepcopy(rows)
        summary = calibrate.make_summary(records, admissions, dict(enumerate(rows)),
                                         methods=methods, seeds=[0, 1], accounting={}, complete=False)
        expected_tasks = []
        for record in records:
            candidate = record["candidate"]
            task_id = candidate["id"]
            selected = [row for row in rows if row["task_id"] == task_id]
            expected_tasks.append({**candidate, **admissions[task_id],
                                   "expected_trials": 6 if admissions[task_id]["admitted"] else 0,
                                   "methods": {method: calibrate.summarize_trials(
                                       [row for row in selected if row["method"] == method]) for method in methods},
                                   "pooled": calibrate.summarize_trials(selected)})
        self.assertEqual(summary["tasks"], expected_tasks)
        self.assertEqual(summary["per_method"], {method: calibrate.summarize_trials(
            [row for row in rows if row["method"] == method]) for method in methods})
        self.assertEqual(summary["pooled"], calibrate.summarize_trials(rows))
        self.assertEqual(summary["remaining_trials"], 8)
        self.assertEqual(rows, original_rows)

    def test_admission_rejects_public_seed_win_and_requires_both_reference_margins(self):
        task = {"constraints": {"gain_db": {"min": 60}}}
        initial, reference, seed = result(50, success=False), result(62, success=True), result(40, success=False)
        self.assertTrue(calibrate.admission_decision(task, initial, reference, seed)["admitted"])
        won = calibrate.admission_decision(task, initial, reference, result(62, success=True))
        self.assertFalse(won["admitted"])
        self.assertEqual(won["public_seed_status"], "passes")
        marginal = deepcopy(reference)
        marginal["independent_metrics"]["gain_db"] = 60.1
        denied = calibrate.admission_decision(task, initial, marginal, seed)
        self.assertFalse(denied["admitted"])
        self.assertTrue(denied["reference_margin"]["python"])
        self.assertFalse(denied["reference_margin"]["native"])

    def test_invalid_seed_allowed_but_invalid_start_or_reference_rejected(self):
        task = {"constraints": {"gain_db": {"min": 60}}}
        initial, reference = result(50, success=False), result(62, success=True)
        invalid = result(0, success=False, status="failed")
        admitted = calibrate.admission_decision(task, initial, reference, invalid)
        self.assertTrue(admitted["admitted"])
        self.assertEqual(admitted["public_seed_status"], "invalid")
        self.assertFalse(calibrate.admission_decision(task, invalid, reference, invalid)["admitted"])
        self.assertFalse(calibrate.admission_decision(task, initial, invalid, invalid)["admitted"])
        self.assertFalse(calibrate.admission_decision(task, reference, reference, invalid)["admitted"])

    def test_checkpoint_partial_directory_and_exact_completed_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "task" / "coordinate_search_0.json"
            path.parent.mkdir()
            identity = {"task": "hash", "reference": "refhash", "seed": 0, "method": "coordinate_search"}
            self.assertIsNone(calibrate.load_checkpoint(path, identity))
            payload = {"success": False, "evaluations_used": 30}
            calibrate.save_checkpoint(path, identity, payload)
            self.assertEqual(calibrate.load_checkpoint(path, identity), payload)
            for field, value in (("seed", 1), ("reference", "changed"), ("task", "changed")):
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    calibrate.load_checkpoint(path, {**identity, field: value})
            record = json.loads(path.read_text())
            record["_checkpoint"]["complete"] = False
            save_json(path, record)
            self.assertIsNone(calibrate.load_checkpoint(path, identity))

    def test_checkpoint_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trial.json"
            calibrate.save_checkpoint(path, {"seed": 0}, {"success": False})
            record = json.loads(path.read_text())
            record["success"] = True
            save_json(path, record)
            with self.assertRaisesRegex(ValueError, "content hash"):
                calibrate.load_checkpoint(path, {"seed": 0})

    def test_ids_reader_supports_selected_manifest_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ids.json"
            save_json(path, {"tasks": [{"id": "one"}, {"id": "two"}]})
            self.assertEqual(calibrate._read_ids(path), {"one", "two"})
            path.write_text("one\ntwo\n")
            self.assertEqual(calibrate._read_ids(path), {"one", "two"})
            save_json(path, ["one", "one"])
            with self.assertRaises(ValueError):
                calibrate._read_ids(path)

    def test_mocked_pipeline_resumes_trials_and_keeps_solver_budget_counts(self):
        class FakeCache:
            def __init__(self, directory, simulator_timeout=30):
                self.directory = Path(directory).resolve()
                self.timeout = simulator_timeout
                self.lock = threading.Lock()
                self.snapshot = object()
                self.physical_evaluations = self.cache_hits = 0
                self.seen = set()

            def assert_unchanged(self):
                pass

            def evaluate(self, task, values):
                key = canonical_hash(values)
                with self.lock:
                    if key in self.seen:
                        self.cache_hits += 1
                    else:
                        self.seen.add(key)
                        self.physical_evaluations += 1
                metrics = {"gain_db": values["W"] * 30}
                return {"status": "ok", "parameters": values, "metrics": metrics,
                        "independent_metrics": dict(metrics), "crosscheck": {"agrees": True},
                        "measurement_key": key, "measurement_directory": "runs/fake/" + key,
                        **score(metrics, task["constraints"])}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = root / "candidates"
            candidates.mkdir()
            save_json(candidates / "index.json", {"tasks": []})
            save_json(root / "catalog.json", {"entries": []})
            save_json(candidates / "candidate.json", {"id": "fixture"})
            task = {"id": "fixture", "max_evaluations": 30,
                    "parameters": {"W": {"min": 1, "max": 4, "unit": "um"}},
                    "initial_parameters": {"W": 1}, "constraints": {"gain_db": {"min": 60}},
                    "circuit_directory": "circuits/fake", "subcircuit": "fake",
                    "conditions": {}, "ac": {}, "transient": {}}
            records = [{"candidate": {"id": "fixture", "topology": "fake"}, "task": task,
                        "reference": {"W": 3}, "public_default": {"W": 1},
                        "path": candidates / "candidate.json"}]
            kwargs = {"catalog_path": root / "catalog.json", "methods": ["uniform_log", "coordinate_search"],
                      "seeds": [0, 1], "workers": 2}
            with patch.object(calibrate, "load_candidates", side_effect=lambda *a, **k: deepcopy(records)), \
                 patch.object(calibrate, "MeasurementCache", FakeCache), \
                 patch.object(calibrate, "measurement_identity", return_value={"fixture_physics": 1}), \
                 patch("sys.stdout", new=io.StringIO()):
                first = calibrate.calibrate(candidates, root / "output", root / "cache", **kwargs)
                second = calibrate.calibrate(candidates, root / "output", root / "cache", **kwargs)
                self.assertEqual(first["completed_trials"], 4)
                self.assertEqual(first["pooled"], second["pooled"])
                self.assertEqual(second["accounting"]["current_invocation"]["physical_evaluations_this_invocation"], 0)
                self.assertEqual(second["accounting"]["current_invocation"]["trial_checkpoints_reused"], 4)
                self.assertGreater(first["pooled"]["logical_evaluations"], 0)
                (root / "output/fixture/uniform_log_1.json").unlink()
                resumed = calibrate.calibrate(candidates, root / "output", root / "cache", **kwargs)
                self.assertEqual(resumed["completed_trials"], 4)
                self.assertEqual(first["pooled"], resumed["pooled"])
                with self.assertRaisesRegex(ValueError, "configuration"):
                    calibrate.calibrate(candidates, root / "output", root / "cache", **{**kwargs, "seeds": [2]})


if __name__ == "__main__":
    unittest.main()
