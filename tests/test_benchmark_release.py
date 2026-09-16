from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.metrics import score
from analog_design.simulator import ROOT as PROJECT_ROOT, digest
from benchmark.verify_release import verify_release


class BenchmarkReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "dataset"
        self.bundle.mkdir()
        circuit = self.root / "circuits/example"
        circuit.mkdir(parents=True)
        original = PROJECT_ROOT / "circuits/autockt_two_stage"
        for name in ("netlist.spice", "reference.params", "source.json"):
            (circuit / name).write_bytes((original / name).read_bytes())
        self.definition = json.loads((PROJECT_ROOT / "tasks/autockt_two_stage_sizing.json").read_text())
        self.definition.update(id="task_1", circuit_directory="circuits/example")
        self.reference = {"CCOMP": 2e-11, "IBIAS": 2.5e-5, "W_GM": 80}
        self.domain = {"name": "example", "family": "miller", "task": deepcopy(self.definition),
                       "public_default": {"CCOMP": 2e-11, "IBIAS": 3e-5, "W_GM": 80},
                       "source": json.loads((circuit / "source.json").read_text())}
        self.domains = self.root / "domains.json"
        self.write(self.domains, {"entries": [self.domain]})
        self.index = {"task_count": 1, "tasks": [self.add_task(self.definition, self.reference)]}
        self.index_path = self.bundle / "index.json"
        self.write(self.index_path, self.index)
        self.output_counter = 0
        self.root_patch = patch("benchmark.verify_release.ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n")

    def add_task(self, definition, reference, *, split="train", group="group_1"):
        name = definition["id"]
        task_path, reference_path = f"tasks/{name}.json", f"private/{name}/reference.json"
        self.write(self.bundle / task_path, definition)
        self.write(self.bundle / reference_path, reference)
        return {"id": name, "path": task_path, "sha256": digest(self.bundle / task_path),
                "reference_path": reference_path, "reference_sha256": digest(self.bundle / reference_path),
                "topology": "example", "topology_family": "miller", "difficulty": "easy",
                "split": split, "requirement_group": group}

    def run_audit(self, **kwargs):
        self.write(self.index_path, self.index)
        self.output_counter += 1
        output = self.root / f"verification_{self.output_counter}"
        report = verify_release(self.index_path, output, domains_path=self.domains, **kwargs)
        self.assertEqual(json.loads((output / "verification.json").read_text()), report)
        return report, output

    def assert_failed(self, fragment, **kwargs):
        report, _ = self.run_audit(**kwargs)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["publication_ready"])
        self.assertIn(fragment, json.dumps(report["errors"]))

    def fake_evaluate(self, task_path, parameters, directory, timeout):
        task = json.loads(task_path.read_text())
        metrics = {"gain_db": 80, "unity_gain_hz": 2e6, "phase_margin_deg": 70,
                   "power_w": 0.002 if directory.name in {"initial", "measurement"} else 0.0003,
                   "dc_error_v": 0.0001, "max_tracking_error_v": 0.0001,
                   "settling_rise_s": 1e-7, "settling_fall_s": 1e-7}
        return {"status": "ok", "metrics": metrics, "independent_metrics": deepcopy(metrics),
                "simulator_invocations": 2, **score(metrics, task["constraints"])}

    def test_static_verification_checks_files_without_claiming_simulation_or_training(self):
        with patch("benchmark.verify_release.evaluate") as evaluate:
            report, _ = self.run_audit(expected_count=1)
        evaluate.assert_not_called()
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["scope"], "static_only")
        self.assertFalse(report["publication_ready"])
        self.assertFalse(report["training_approved"])
        self.assertFalse(report["difficulty_validated"])
        self.assertEqual(report["unique_reference_count"], 1)

    def test_fresh_simulation_uses_original_requirements_and_refines_reference(self):
        calls = []

        def evaluator(path, parameters, directory, timeout):
            calls.append((json.loads(path.read_text()), deepcopy(parameters), directory.name))
            return self.fake_evaluate(path, parameters, directory, timeout)

        with patch("benchmark.verify_release.evaluate", side_effect=evaluator), \
             patch("benchmark.verify_release._runtime_identity", return_value={}):
            report, output = self.run_audit(simulate=True)
        self.assertEqual(report["status"], "verified")
        self.assertTrue(report["publication_ready"])
        self.assertEqual(calls[0][2], "measurement")
        self.assertEqual(calls[0][1], self.domain["public_default"])
        self.assertEqual(report["public_default_audit"]["status"], "verified")
        calls = calls[1:]
        self.assertEqual([call[2] for call in calls], ["initial", "reference", "refined_reference"])
        self.assertEqual(calls[0][1], {})
        self.assertEqual(calls[1][1], self.reference)
        self.assertEqual(calls[2][1], self.reference)
        self.assertEqual(calls[2][0]["constraints"], self.definition["constraints"])
        self.assertEqual(calls[2][0]["ac"]["points_per_decade"], 2 * self.definition["ac"]["points_per_decade"])
        self.assertEqual(calls[2][0]["transient"]["max_step_s"], self.definition["transient"]["max_step_s"] / 2)
        self.assertTrue((output / "tasks/task_1/refined_reference_returned_result.json").is_file())

    def test_expected_count_and_hash_mismatches_fail(self):
        self.assert_failed("Expected 250", expected_count=250)
        self.index["tasks"][0]["reference_sha256"] = "0" * 64
        self.assert_failed("Artifact content hash disagrees")

    def test_duplicate_ids_and_reference_vectors_fail(self):
        self.index["tasks"].append(deepcopy(self.index["tasks"][0]))
        self.index["task_count"] = 2
        self.assert_failed("Duplicate task ID")
        second = deepcopy(self.definition)
        second["id"] = "task_2"
        second["initial_parameters"]["IBIAS"] = 4e-5
        self.index["tasks"][1] = self.add_task(second, self.reference)
        self.assert_failed("Duplicate reference vector")

    def test_recomputed_requirements_detect_split_leakage_despite_different_group_labels(self):
        second = deepcopy(self.definition)
        second["id"] = "task_2"
        second["initial_parameters"]["IBIAS"] = 4e-5
        reference = {**self.reference, "CCOMP": 1.9e-11}
        self.index["tasks"].append(self.add_task(second, reference, split="test", group="forged_group"))
        self.index["task_count"] = 2
        self.assert_failed("Related requirements occur in different splits")

    def test_missing_metrics_bad_budget_and_out_of_bounds_reference_fail(self):
        changed = deepcopy(self.definition)
        del changed["constraints"]["power_w"]
        self.index["tasks"][0] = self.add_task(changed, self.reference)
        self.assert_failed("exactly all eight")
        changed = deepcopy(self.definition)
        changed["max_evaluations"] = 31
        self.index["tasks"][0] = self.add_task(changed, self.reference)
        self.assert_failed("30-evaluation budget")
        self.index["tasks"][0] = self.add_task(self.definition, {**self.reference, "W_GM": 1000})
        self.assert_failed("outside valid bounds")

    def test_nonfinite_and_nonintegral_reference_fail(self):
        self.index["tasks"][0] = self.add_task(self.definition, {**self.reference, "W_GM": float("nan")})
        self.assert_failed("Invalid JSON number")
        definition = deepcopy(self.definition)
        definition["parameters"]["W_GM"]["integer"] = True
        self.index["tasks"][0] = self.add_task(definition, {**self.reference, "W_GM": 80.5})
        self.assert_failed("must be integral")

    def test_changed_conditions_and_source_files_fail_before_simulating(self):
        definition = deepcopy(self.definition)
        definition["conditions"]["supply_v"] = 1.7
        self.index["tasks"][0] = self.add_task(definition, self.reference)
        with patch("benchmark.verify_release.evaluate") as evaluate:
            self.assert_failed("conditions disagrees", simulate=True)
        evaluate.assert_not_called()
        self.index["tasks"][0] = self.add_task(self.definition, self.reference)
        with (self.root / "circuits/example/netlist.spice").open("a") as stream:
            stream.write("* changed\n")
        self.assert_failed("pinned source hashes")

    def test_measurement_disagreement_and_failing_refinement_cannot_publish(self):
        def ambiguous(path, parameters, directory, timeout):
            result = self.fake_evaluate(path, parameters, directory, timeout)
            if directory.name == "initial":
                result["metrics"]["phase_margin_deg"] = 59.99
                result["independent_metrics"]["phase_margin_deg"] = 60.01
                result.update(score(result["metrics"], self.definition["constraints"]))
            return result

        with patch("benchmark.verify_release.evaluate", side_effect=ambiguous), \
             patch("benchmark.verify_release._runtime_identity", return_value={}):
            self.assert_failed("per-constraint decisions disagree", simulate=True)

        def failed_refinement(path, parameters, directory, timeout):
            result = self.fake_evaluate(path, parameters, directory, timeout)
            if directory.name == "refined_reference":
                result.update(status="timeout", success=False, reward=-1)
            return result

        with patch("benchmark.verify_release.evaluate", side_effect=failed_refinement), \
             patch("benchmark.verify_release._runtime_identity", return_value={}):
            self.assert_failed("did not produce valid measurements", simulate=True)

    def test_public_context_editable_values_and_unbacked_approval_are_rejected(self):
        definition = deepcopy(self.definition)
        definition["design_context"] = {
            "topology": "example", "family": "miller", "paper": {"title": "Fixture"},
            "netlist": (self.root / "circuits/example/netlist.spice").read_text(),
            "fixed_parameters": {"W_GM": 80}, "scope": "Test fixture"}
        self.index["tasks"][0] = self.add_task(definition, self.reference)
        self.assert_failed("expose an editable parameter")
        self.index["tasks"][0] = self.add_task(self.definition, self.reference)
        self.index["training_approved"] = True
        with patch("benchmark.verify_release.require_training_approval", side_effect=RuntimeError("Training is not approved")):
            self.assert_failed("Training is not approved")

    def test_paths_and_existing_output_are_rejected(self):
        self.index["tasks"][0]["path"] = "../outside.json"
        self.assert_failed("release layout")
        with self.assertRaises(FileExistsError):
            verify_release(self.index_path, self.root, domains_path=self.domains)
        with self.assertRaises(ValueError):
            verify_release(self.index_path, self.root / "unused", workers=5)

    def test_public_default_solution_rejects_even_when_all_task_references_pass(self):
        def evaluator(path, parameters, directory, timeout):
            result = self.fake_evaluate(path, parameters, directory, timeout)
            if directory.name == "measurement":
                result["metrics"]["power_w"] = 0.0003
                result["independent_metrics"]["power_w"] = 0.0003
                result.update(score(result["metrics"], json.loads(path.read_text())["constraints"]))
            return result

        with patch("benchmark.verify_release.evaluate", side_effect=evaluator), \
             patch("benchmark.verify_release._runtime_identity", return_value={}):
            report, output = self.run_audit(simulate=True)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["publication_ready"])
        self.assertEqual(report["public_default_audit"]["tasks_solved"], ["task_1"])
        self.assertEqual(report["tasks"][0]["status"], "verified")
        self.assertTrue((output / "public_defaults/public_default_example/returned_result.json").is_file())

    def test_invalid_public_default_is_explicitly_unresolved(self):
        def evaluator(path, parameters, directory, timeout):
            if directory.name == "measurement":
                return {"status": "timeout", "success": False, "reward": -1,
                        "metrics": {}, "error": "test timeout"}
            return self.fake_evaluate(path, parameters, directory, timeout)

        with patch("benchmark.verify_release.evaluate", side_effect=evaluator), \
             patch("benchmark.verify_release._runtime_identity", return_value={}):
            report, _ = self.run_audit(simulate=True)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["publication_ready"])
        self.assertEqual(report["public_default_audit"]["status"], "unresolved")
        self.assertEqual(report["public_default_audit"]["unresolved_tasks"], ["task_1"])
        self.assertIsNone(report["public_default_audit"]["task_outcomes"]["task_1"]["success"])
        self.assertIn("test timeout", json.dumps(report["public_default_audit"]))

    def test_reference_distances_reject_near_duplicates_and_coverage_reports_shared_solutions(self):
        second = deepcopy(self.definition)
        second["id"] = "task_2"
        second["initial_parameters"]["IBIAS"] = 4e-5
        self.index["tasks"].append(self.add_task(second, {**self.reference, "CCOMP": 1.999e-11}))
        self.index["task_count"] = 2
        self.assert_failed("minimum normalized distance")
        self.index["tasks"][1] = self.add_task(second, {"CCOMP": 1e-11, "IBIAS": 3e-5, "W_GM": 60})
        with patch("benchmark.verify_release.evaluate", side_effect=self.fake_evaluate) as evaluate, \
             patch("benchmark.verify_release._runtime_identity", return_value={}):
            report, _ = self.run_audit(simulate=True)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(evaluate.call_count, 7)  # One public default, three runs per task.
        distance = report["reference_diversity"]["topologies"]["example"]
        self.assertGreater(distance["minimum_observed_distance"], 0.025)
        self.assertEqual(distance["nearest_references"]["task_1"]["reference_id"], "task_2")
        coverage = report["reference_solution_coverage"]["topologies"]["example"]
        self.assertEqual(coverage["greedy_cover_count"], 1)
        self.assertEqual(coverage["maximum_tasks_solved_by_one_reference"], 2)
        self.assertEqual(coverage["uncovered_task_ids"], [])
        self.assertTrue(all(row["tasks_solved_count"] == 2 for row in coverage["reference_coverage"]))


if __name__ == "__main__":
    unittest.main()
