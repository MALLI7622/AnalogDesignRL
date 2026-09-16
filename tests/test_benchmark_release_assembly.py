"""Release-integrity regressions; fixtures never launch ngspice or alter inputs."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.metrics import score
from analog_design.simulator import ROOT, digest
from benchmark.baselines import METHODS, run_search
from benchmark.calibrate import CODE_FILES, admission_decision, save_checkpoint, summarize_trials
from benchmark.explore import rounded_parameters
from benchmark.release import (CalibrationEvidence, InputSnapshot, assemble, candidate_records,
                               validate_trial)
from benchmark.simulation import canonical_hash, save_json


def definition():
    domain = deepcopy(json.loads((ROOT / "benchmark/domains.json").read_text())["entries"][0])
    task = deepcopy(domain["task"])
    task["id"] = "assembly_fixture"
    task["initial_parameters"] = rounded_parameters(task, task["initial_parameters"])
    return task, domain


def measured(task, passed, parameters=None):
    metrics = {"gain_db": 90, "unity_gain_hz": 4e6, "phase_margin_deg": 80,
               "power_w": 0.0001 if passed else 0.002,
               "dc_error_v": 0.0001, "max_tracking_error_v": 0.0001,
               "settling_rise_s": 1e-7, "settling_fall_s": 1e-7}
    return {"status": "ok", "metrics": metrics, "independent_metrics": deepcopy(metrics),
            "parameters": parameters or task["initial_parameters"], **score(metrics, task["constraints"])}


def trial(task, method=METHODS[0], seed=0, success_call=3):
    calls = 0

    def evaluate(parameters):
        nonlocal calls
        calls += 1
        return measured(task, calls == success_call, parameters)

    result = run_search(task, evaluate, method, seed)
    for step in result["trajectory"]:
        step["independent_metrics"] = deepcopy(step["metrics"])
    return result


class TrialIntegrityTests(unittest.TestCase):
    def test_recorded_trials_replay_under_the_declared_method_seed_and_budget(self):
        task, _ = definition()
        for method in METHODS:
            valid = trial(task, method)
            self.assertEqual(validate_trial(valid, task, method, 0), valid)

    def test_forged_trial_counts_wrong_ids_seeds_and_actions_fail(self):
        task, _ = definition()
        valid = trial(task)
        variants = []
        for field, value in (("evaluations_used", 20), ("max_evaluations", 100),
                             ("task_id", "other"), ("method", METHODS[-1]), ("seed", 20), ("seed", False)):
            changed = deepcopy(valid)
            changed[field] = value
            variants.append(changed)
        changed = deepcopy(valid)
        changed["trajectory"][1]["parameters"] = deepcopy(changed["trajectory"][0]["parameters"])
        variants.append(changed)
        changed = deepcopy(valid)
        changed["trajectory"].append(deepcopy(changed["trajectory"][-1]))
        variants.append(changed)
        changed = deepcopy(valid)
        changed["trajectory"][0]["reward"] = 1
        variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed.keys()), self.assertRaises(ValueError):
                validate_trial(changed, task, METHODS[0], 0)

    def test_trial_native_measurement_disagreement_fails(self):
        task, _ = definition()
        record = trial(task)
        record["trajectory"][0]["independent_metrics"]["gain_db"] += 5
        with self.assertRaisesRegex(ValueError, "measurement paths disagree"):
            validate_trial(record, task, METHODS[0], 0)


class AssemblyPreflightTests(unittest.TestCase):
    def test_exact_level_counts_and_distinct_ids_are_checked_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = root / "selection.json"
            rows = [{"id": f"task_{index}", "difficulty": "hard"} for index in range(250)]
            save_json(selection, {"status": "selection_complete", "tasks": rows})
            with self.assertRaisesRegex(ValueError, "100 easy, 125 medium, and 25 hard"):
                assemble(root / "missing_candidates", selection, root / "output")
            self.assertFalse((root / "output").exists())
            for index, row in enumerate(rows):
                row["difficulty"] = "easy" if index < 100 else "medium" if index < 225 else "hard"
            rows[-1]["id"] = rows[0]["id"]
            save_json(selection, {"status": "selection_complete", "tasks": rows})
            with self.assertRaisesRegex(ValueError, "250 distinct"):
                assemble(root / "missing_candidates", selection, root / "output")
            self.assertFalse((root / "output").exists())

    def test_candidate_paths_and_duplicate_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for entries in ([{"id": "task", "path": "../outside.json"}],
                            [{"id": "../escape", "path": "candidates/../escape.json"}],
                            [{"id": "task", "path": "candidates/task.json"}] * 2):
                save_json(root / "index.json", {"tasks": entries})
                with self.assertRaises(ValueError):
                    candidate_records(root, InputSnapshot())

    def test_snapshot_rejects_mutation_after_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            save_json(path, {"complete": True})
            snapshot = InputSnapshot()
            snapshot.read(path)
            save_json(path, {"complete": False})
            with self.assertRaisesRegex(ValueError, "Input changed"):
                snapshot.assert_unchanged()

    def test_calibration_manifest_requires_real_frozen_input_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates, calibration = root / "candidates", root / "calibration"
            candidate = candidates / "candidates/fixture.json"
            save_json(candidate, {})
            save_json(candidates / "index.json", {"tasks": [{"id": "fixture", "path": "candidates/fixture.json"}]})
            save_json(calibration / "manifest.json", {})
            with self.assertRaisesRegex(ValueError, "missing its configuration"):
                CalibrationEvidence(candidates, calibration)
            config = {"methods": list(METHODS), "seeds": [0], "budget": 30, "simulator_timeout_s": 30,
                      "candidate_ids": ["fixture"], "inputs_sha256": {}, "physics": {},
                      "cache_directory": str(root / "cache")}
            save_json(calibration / "manifest.json", {"schema_version": 1, "configuration": config,
                "configuration_sha256": canonical_hash(config), "physical_identities": {}})
            with self.assertRaisesRegex(ValueError, "required input binding"):
                CalibrationEvidence(candidates, calibration)
            paths = [candidates / "index.json", candidate, ROOT / "benchmark/domains.json",
                     *[ROOT / name for name in CODE_FILES]]
            config["inputs_sha256"] = {str(path.resolve()): digest(path) for path in paths}
            config["inputs_sha256"][str((ROOT / CODE_FILES[0]).resolve())] = "0" * 64
            save_json(calibration / "manifest.json", {"schema_version": 1, "configuration": config,
                "configuration_sha256": canonical_hash(config), "physical_identities": {}})
            with self.assertRaisesRegex(ValueError, "Input content hash disagrees"):
                CalibrationEvidence(candidates, calibration)


class CheckpointAdmissionTests(unittest.TestCase):
    """Exercise real checkpoint validation while mocking physical cache lookup.

    These fixtures test checkpoint/task binding and trial reconstruction. Physical
    cache lookup is tested separately; no fixture claims simulator feasibility.
    """
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.task, self.domain = definition()
        self.name = self.task["id"]
        self.reference = rounded_parameters(self.task, {**self.task["initial_parameters"], "IBIAS": 2.5e-5})
        candidate = {"id": self.name, "topology": self.domain["name"], "topology_family": self.domain["family"],
                     "task": self.task, "reference": self.reference}
        path = self.root / "candidates" / (self.name + ".json")
        save_json(path, candidate)
        self.evidence = CalibrationEvidence.__new__(CalibrationEvidence)
        self.evidence.snapshot = InputSnapshot()
        self.evidence.records = {self.name: {"absolute_path": path, "topology": self.domain["name"]}}
        self.evidence.directory = self.root / "calibration"
        self.evidence.config = {"candidate_ids": [self.name]}
        self.evidence.configuration_sha256 = "f" * 64
        self.evidence.methods = list(METHODS)
        self.evidence.seeds = list(range(10))
        self.evidence.domains = {self.domain["name"]: self.domain}
        physical_key = canonical_hash({key: self.task[key] for key in
            ("circuit_directory", "subcircuit", "conditions", "parameters", "ac", "transient")})
        physics = {"fixture": "physical cache lookup is mocked"}
        self.evidence.physical_identities = {physical_key: physics}
        self.identity = {"configuration_sha256": self.evidence.configuration_sha256,
                         "candidate_sha256": digest(path), "task_sha256": canonical_hash(self.task),
                         "reference_sha256": canonical_hash(self.reference),
                         "public_default_sha256": canonical_hash(self.domain["public_default"]),
                         "physics_sha256": canonical_hash(physics)}
        initial = measured(self.task, False)
        reference = measured(self.task, True, self.reference)
        public = measured(self.task, False, self.domain["public_default"])
        self.admission = {"id": self.name, "topology": self.domain["name"], "effective_task": self.task,
                          "rounded_reference_parameters": self.reference,
                          "public_default_parameters": self.domain["public_default"], "logical_evaluations": 3,
                          "evaluations": {"initial": initial, "reference": reference, "public_seed": public},
                          **admission_decision(self.task, initial, reference, public)}
        self.admission_path = self.evidence.directory / self.name / "admission.json"
        save_checkpoint(self.admission_path, {**self.identity, "kind": "admission"}, self.admission)
        self.trials, methods = [], {}
        for method in METHODS:
            rows = [trial(self.task, method, seed) for seed in self.evidence.seeds]
            for row in rows:
                save_checkpoint(self.evidence.directory / self.name / f"{method}_{row['seed']}.json",
                                {**self.identity, "kind": "baseline", "method": method, "seed": row["seed"]}, row)
            self.trials.extend(rows)
            methods[method] = summarize_trials(rows)
        self.evidence.summary_rows = {self.name: {"id": self.name, "admitted": True,
            "topology": self.domain["name"], "topology_family": self.domain["family"],
            "rejection_reasons": [], "expected_trials": len(METHODS) * len(self.evidence.seeds),
            "methods": methods, "pooled": summarize_trials(self.trials)}}
        self.measurement_patch = patch.object(self.evidence, "_measurement")
        self.measurement_patch.start()
        self.addCleanup(self.measurement_patch.stop)

    def test_complete_bound_checkpoints_reconstruct_actual_method_statistics(self):
        audited = self.evidence.task(self.name)
        self.assertEqual(audited["level"], "easy")
        self.assertEqual(len(audited["checkpoint_files"]), 1 + 10 * len(METHODS))
        self.assertTrue(all(row["completed_trials"] == 10 for row in audited["observation"]["methods"].values()))

    def test_forged_summary_is_rejected_despite_valid_checkpoint_files(self):
        self.evidence.summary_rows[self.name]["methods"][METHODS[0]]["completed_trials"] = 20
        with self.assertRaisesRegex(ValueError, "summary statistics disagree"):
            self.evidence.task(self.name)

    def test_ten_real_failed_trials_per_method_remain_hard_candidates(self):
        methods, all_trials = {}, []
        for method in METHODS:
            rows = [trial(self.task, method, seed, success_call=31) for seed in self.evidence.seeds]
            for row in rows:
                save_checkpoint(self.evidence.directory / self.name / f"{method}_{row['seed']}.json",
                                {**self.identity, "kind": "baseline", "method": method, "seed": row["seed"]}, row)
            methods[method] = summarize_trials(rows)
            all_trials.extend(rows)
        self.evidence.summary_rows[self.name].update(methods=methods, pooled=summarize_trials(all_trials))
        audited = self.evidence.task(self.name)
        self.assertEqual(audited["level"], "hard_candidate")
        self.assertTrue(all(value["completed_trials"] == 10 for value in audited["observation"]["methods"].values()))

    def test_missing_trial_is_rejected_without_substituting_summary_count(self):
        path = self.evidence.directory / self.name / f"{METHODS[0]}_0.json"
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing or incomplete baseline"):
            self.evidence.task(self.name)

    def test_tampered_payload_identity_or_incomplete_checkpoint_is_rejected(self):
        raw = json.loads(self.admission_path.read_text())
        changed = deepcopy(raw)
        changed["logical_evaluations"] = 2
        save_json(self.admission_path, changed)
        with self.assertRaisesRegex(ValueError, "content hash disagrees"):
            self.evidence.task(self.name)
        self.evidence.snapshot = InputSnapshot()
        changed = deepcopy(raw)
        changed["_checkpoint"]["identity"]["task_sha256"] = "wrong"
        save_json(self.admission_path, changed)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.evidence.task(self.name)
        self.evidence.snapshot = InputSnapshot()
        changed = deepcopy(raw)
        changed["_checkpoint"]["complete"] = False
        save_json(self.admission_path, changed)
        with self.assertRaisesRegex(ValueError, "Missing or incomplete admission"):
            self.evidence.task(self.name)

    def test_internally_hashed_but_false_admission_decision_is_rejected(self):
        changed = deepcopy(self.admission)
        changed["reference_margin"]["python"] = False
        save_checkpoint(self.admission_path, {**self.identity, "kind": "admission"}, changed)
        with self.assertRaisesRegex(ValueError, "recomputed decisions"):
            self.evidence.task(self.name)


if __name__ == "__main__":
    unittest.main()
