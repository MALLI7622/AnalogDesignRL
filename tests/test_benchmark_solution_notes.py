from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.metrics import score
from benchmark import solution_notes as notes
from benchmark.verify_release import _check_measurement


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(root):
    bundle, verification = root / "bundle", root / "verification/verification.json"
    entries, results, captured = [], [], {}
    for index in range(2):
        task_id = "task_" + str(index)
        task = {"id": task_id, "parameters": {"W": {"min": 1, "max": 10, "unit": "um", "role": "Input-pair width"}},
                "initial_parameters": {"W": 2}, "ac": {"points_per_decade": 100}, "transient": {"max_step_s": 5e-9},
                "constraints": {"gain_db": {"min": 60}, "unity_gain_hz": {"min": 1e6}, "phase_margin_deg": {"min": 60},
                                "power_w": {"max": 1e-3}, "dc_error_v": {"max": 0.002}, "max_tracking_error_v": {"max": 0.002},
                                "settling_rise_s": {"max": 1e-6}, "settling_fall_s": {"max": 1e-6}}}
        reference = {"W": 4 + index}
        task_path, ref_path = bundle / "tasks" / (task_id + ".json"), bundle / "private" / task_id / "reference.json"
        task_hash, ref_hash = save(task_path, task), save(ref_path, reference)
        captured.update({str(task_path): task_hash, str(ref_path): ref_hash})
        entries.append({"id": task_id, "topology": "ota", "path": str(task_path.relative_to(bundle)), "sha256": task_hash,
                        "reference_path": str(ref_path.relative_to(bundle)), "reference_sha256": ref_hash})
        run = verification.parent / "tasks" / task_id
        copied_hash = save(run / "task.json", task)
        refined = deepcopy(task)
        refined["ac"]["points_per_decade"] *= 2
        refined["transient"]["max_step_s"] /= 2
        refined_hash = save(run / "refined_task.json", refined)
        record = {"id": task_id, "status": "verified", "errors": [], "evaluations": {}}
        for phase, expected in notes.PHASES:
            metrics = {"gain_db": 61, "unity_gain_hz": 1.1e6, "phase_margin_deg": 65, "power_w": 0.0008,
                       "dc_error_v": 0.001, "max_tracking_error_v": 0.001,
                       "settling_rise_s": 0.8e-6, "settling_fall_s": 0.8e-6}
            if phase == "initial":
                metrics.update(gain_db=59, power_w=0.0012)
            raw = {"task_id": task_id, "task_sha256": refined_hash if phase == "refined_reference" else copied_hash,
                   "parameters": task["initial_parameters"] if phase == "initial" else reference,
                   "status": "ok", "metrics": metrics, "independent_metrics": deepcopy(metrics),
                   "simulator_invocations": 2, **score(metrics, task["constraints"])}
            save(run / (phase + "_returned_result.json"), raw)
            (run / phase).mkdir()
            record["evaluations"][phase] = _check_measurement(raw, refined if phase == "refined_reference" else task, expected)
        save(run / "verification.json", record)
        results.append(record)
    index_hash = save(bundle / "index.json", {"task_count": 2, "tasks": entries})
    captured[str(bundle / "index.json")] = index_hash
    save(verification, {"task_count": 2, "tasks": results, "status": "verified", "publication_ready": True,
                        "scope": "fresh_simulation_and_static", "simulations_requested": True, "errors": [],
                        "inputs_sha256": captured, "index_path": str(bundle / "index.json"), "finished_utc": "test_time",
                        "runtime_identities": {"ota": {"runtime": {"ngspice_version": "synthetic test evidence"},
                                                       "evaluator_sha256": {}, "spice_dependency_sha256": {}}}})
    return bundle, verification


class SolutionNoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle, self.verification = fixture(self.root)

    def assert_no_notes(self):
        self.assertEqual(list(self.bundle.rglob("SOLUTION.md")), [])
        self.assertFalse((self.bundle / "private/solution_notes_manifest.json").exists())

    def test_complete_private_notes_show_verified_vectors_slack_and_hash_links(self):
        public_before = {path: path.read_bytes() for path in self.bundle.glob("tasks/*.json")}
        result = notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assertEqual(result["task_count"], 2)
        self.assertEqual(result["status"], "complete")
        for row in result["notes"]:
            path = self.bundle / row["path"]
            self.assertTrue(path.is_relative_to(self.bundle / "private"))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])
            text = path.read_text()
            self.assertIn("one verified feasible parameter vector", text)
            self.assertIn("Input-pair width", text)
            self.assertIn("fails exactly: `gain_db`, `power_w`", text)
            self.assertIn("passes all 8 requirements", text)
            self.assertIn("Reference worst-path slack", text)
            self.assertIn("do not isolate the causal effect", text)
            self.assertIn("reference_returned_result.json", text)
            self.assertIn("not an optimum", text)
        self.assertEqual({path: path.read_bytes() for path in public_before}, public_before)
        again = notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assertEqual(again, result)

    def test_missing_last_task_or_nonfresh_report_aborts_before_any_note(self):
        original = json.loads(self.verification.read_text())
        for change in ("missing_task", "static", "failed"):
            with self.subTest(change=change):
                value = deepcopy(original)
                if change == "missing_task":
                    value["tasks"].pop()
                elif change == "static":
                    value["scope"] = "static_only"
                else:
                    value["tasks"][-1]["status"] = "failed"
                save(self.verification, value)
                with self.assertRaises(ValueError):
                    notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
                self.assert_no_notes()

    def test_exact_parameter_binding_and_report_copy_agreement_are_required(self):
        path = self.verification.parent / "tasks/task_1/reference_returned_result.json"
        raw = json.loads(path.read_text())
        raw["parameters"] = {"W": 9}
        save(path, raw)
        with self.assertRaisesRegex(ValueError, "parameter vector"):
            notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assert_no_notes()
        self.bundle, self.verification = fixture(self.root / "other")
        audit = json.loads(self.verification.read_text())
        audit["tasks"][1]["evaluations"]["reference"]["metrics"]["gain_db"] = 62
        save(self.verification, audit)
        with self.assertRaisesRegex(ValueError, "Per-task verification record differs"):
            notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assert_no_notes()

    def test_mutated_index_or_reference_hash_aborts_before_writes(self):
        path = self.bundle / "private/task_1/reference.json"
        save(path, {"W": 7})
        with self.assertRaisesRegex(ValueError, "hash changed"):
            notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assert_no_notes()
        self.bundle, self.verification = fixture(self.root / "other")
        index = json.loads((self.bundle / "index.json").read_text())
        index["changed"] = True
        save(self.bundle / "index.json", index)
        with self.assertRaisesRegex(ValueError, "exact bundle index"):
            notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assert_no_notes()

    def test_input_change_during_rendering_and_conflicting_note_fail_before_new_writes(self):
        original = notes.render_solution
        def mutate(record):
            self.verification.write_text(self.verification.read_text() + "\n")
            return original(record)
        with patch.object(notes, "render_solution", side_effect=mutate), self.assertRaisesRegex(ValueError, "input changed"):
            notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assert_no_notes()
        self.bundle, self.verification = fixture(self.root / "other")
        existing = self.bundle / "private/task_1/SOLUTION.md"
        existing.write_text("Human notes to preserve")
        with self.assertRaisesRegex(ValueError, "existing private solution note differs"):
            notes.write_solution_notes(self.bundle, self.verification, expected_count=2)
        self.assertFalse((self.bundle / "private/task_0/SOLUTION.md").exists())
        self.assertEqual(existing.read_text(), "Human notes to preserve")


if __name__ == "__main__":
    unittest.main()
