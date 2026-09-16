from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.metrics import score
from analog_design.simulator import digest
from benchmark.baselines import run_search
from benchmark.calibrate import PHYSICAL_FIELDS, admission_decision, save_checkpoint
from benchmark.research import trajectory_overlap as overlap
from benchmark.simulation import canonical_hash, save_json
from benchmark.verify_release import _hash


def measured(task, gain, native_gain=None):
    metrics = {"gain_db": gain, "unity_gain_hz": 1.1e6, "phase_margin_deg": 65, "power_w": 0.0008,
               "dc_error_v": 0.001, "max_tracking_error_v": 0.001,
               "settling_rise_s": 0.8e-6, "settling_fall_s": 0.8e-6}
    native = {**metrics, "gain_db": gain if native_gain is None else native_gain}
    return {"status": "ok", "metrics": metrics, "independent_metrics": native, **score(metrics, task["constraints"])}


def fixture(root):
    candidates, calibration = root / "candidates", root / "calibration"
    physical = {"circuit_directory": "circuits/ota", "subcircuit": "ota", "conditions": {"supply_v": 1.8},
                "parameters": {"W": {"min": 1, "max": 10, "unit": "um"}},
                "ac": {"points_per_decade": 100}, "transient": {"max_step_s": 5e-9}}
    constraints = {"gain_db": {"min": 60}, "unity_gain_hz": {"min": 1e6}, "phase_margin_deg": {"min": 60},
                   "power_w": {"max": 1e-3}, "dc_error_v": {"max": 0.002}, "max_tracking_error_v": {"max": 0.002},
                   "settling_rise_s": {"max": 1e-6}, "settling_fall_s": {"max": 1e-6}}
    template = {**physical, "id": "template", "initial_parameters": {"W": 2.0}, "constraints": constraints, "max_evaluations": 30}
    default = {"W": 1.5}
    domain_path = root / "benchmark/domains.json"
    save_json(domain_path, {"entries": [{"name": "ota", "task": template, "public_default": default}]})
    definitions, targets, entries = [], [], []
    for name, minimum, reference in (("admitted", 60, {"W": 4.0}), ("rejected", 61.005, {"W": 5.0})):
        task = deepcopy(template)
        task["id"] = name
        task["constraints"]["gain_db"]["min"] = minimum
        target_key = canonical_hash(reference)
        candidate = {"id": name, "topology": "ota", "task": task, "reference": reference, "target_key": target_key}
        relative = "candidates/" + name + ".json"
        save_json(candidates / relative, candidate)
        entries.append({"id": name, "topology": "ota", "path": relative})
        definitions.append(candidate)
        targets.append({"topology": "ota", "key": target_key, "constraints": task["constraints"], "parameters": reference})
    save_json(candidates / "index.json", {"tasks": entries})
    save_json(candidates / "targets.json", {"targets": targets})
    save_json(candidates / "curation_report.json", {"topologies": [{"topology": "ota", "coverage": {
        "target_keys_in_bit_order": [row["key"] for row in targets],
        "coverage_bitmasks_hex": {"old_one": "0x1", "old_two": "0x2"}}}]})
    physical_key = canonical_hash({key: template[key] for key in PHYSICAL_FIELDS})
    inputs = {str(path): digest(path) for path in (domain_path, candidates / "index.json",
                                                  *[candidates / row["path"] for row in entries])}
    config = {"methods": ["uniform_linear"], "seeds": [0], "candidate_ids": [row["id"] for row in entries],
              "physics": {physical_key: "physics_fixture"}, "inputs_sha256": inputs}
    config_hash = canonical_hash(config)
    save_json(calibration / "manifest.json", {"configuration": config, "configuration_sha256": config_hash})
    for candidate, entry in zip(definitions, entries):
        name, task, reference = candidate["id"], candidate["task"], candidate["reference"]
        identity = {"configuration_sha256": config_hash, "candidate_sha256": digest(candidates / entry["path"]),
                    "task_sha256": canonical_hash(task), "reference_sha256": canonical_hash(reference),
                    "public_default_sha256": canonical_hash(default), "physics_sha256": "physics_fixture"}
        evaluations = {"initial": measured(task, 59), "reference": measured(task, 62 if name == "admitted" else 60.5),
                       "public_seed": measured(task, 59)}
        admission = {"id": name, "topology": "ota", "effective_task": task,
                     "rounded_reference_parameters": reference, "public_default_parameters": default, "logical_evaluations": 3,
                     "evaluations": evaluations, **admission_decision(task, **evaluations)}
        save_checkpoint(calibration / name / "admission.json", {**identity, "kind": "admission"}, admission)
        if name == "admitted":
            observed = []
            def evaluate(parameters):
                result = measured(task, 59) if not observed else measured(task, 61.009, 61.001)
                result.update(measurement_key=canonical_hash(parameters), measurement_directory="runs/synthetic")
                observed.append(result)
                return result
            trial = run_search(task, evaluate, "uniform_linear", 0, budget=30)
            for row, result in zip(trial["trajectory"], observed):
                row.update({key: result[key] for key in ("independent_metrics", "measurement_key", "measurement_directory")})
            save_checkpoint(calibration / name / "uniform_linear_0.json", {**identity, "kind": "baseline", "method": "uniform_linear", "seed": 0}, trial)
    return candidates, calibration, targets


class TrajectoryOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.candidates, self.calibration, self.targets = fixture(self.root)
        self.patch_root = patch.object(overlap, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def test_rejected_admission_needs_no_trials_and_all_zero_masks_are_saved(self):
        result = overlap.run(self.candidates, [self.calibration], ["ota"])
        row = result["topologies"]["ota"]
        self.assertEqual(row["status"], "diagnostic_complete")
        self.assertTrue(row["checkpoint_set_complete"])
        self.assertEqual((row["configured_candidates"], row["admitted_candidates"], row["rejected_candidates"]), (2, 1, 1))
        self.assertEqual(row["expected_admitted_trial_files"], 1)
        self.assertEqual(row["trial_files_read"], 1)
        self.assertEqual(row["target_keys_in_bit_order"], [target["key"] for target in self.targets])
        agreed = row["trajectory_agreed_coverage_bitmasks_hex"]
        potential = row["trajectory_either_path_coverage_bitmasks_hex"]
        aliases = row["trajectory_witness_original_hashes"]
        self.assertEqual(set(agreed), set(potential))
        self.assertEqual(set(agreed), set(aliases))
        self.assertEqual(aliases[_hash({"W": 2})], [canonical_hash({"W": 2.0})])
        self.assertNotEqual(canonical_hash({"W": 2.0}), _hash({"W": 2}))
        self.assertEqual(len(agreed), row["unique_valid_parameter_vectors"])
        self.assertEqual(agreed[_hash({"W": 2})], "0x0")
        self.assertEqual(potential[_hash({"W": 2})], "0x0")
        self.assertIn("0x1", agreed.values())
        self.assertIn("0x3", potential.values())
        self.assertTrue(all(int(agreed[key], 16) & ~int(potential[key], 16) == 0 for key in agreed))
        self.assertIn("Conservative potential overlap", row["trajectory_mask_semantics"]["either_path"])
        for path in (self.candidates / "index.json", self.candidates / "targets.json",
                     self.calibration / "rejected/admission.json", self.calibration / "admitted/uniform_linear_0.json"):
            self.assertEqual(result["inputs_sha256"][str(path)], digest(path))
        self.assertEqual(result["simulations_performed"], 0)
        self.assertEqual(result["cache_writes"], 0)

    def test_truly_missing_admitted_trial_is_incomplete(self):
        (self.calibration / "admitted/uniform_linear_0.json").unlink()
        row = overlap.run(self.candidates, [self.calibration], ["ota"])["topologies"]["ota"]
        self.assertEqual(row["status"], "skipped_incomplete_checkpoint_set")
        self.assertFalse(row["checkpoint_set_complete"])
        self.assertEqual(row["missing_admitted_trial_files"], 1)
        self.assertEqual(row["rejected_candidates"], 1)
        self.assertEqual(row["expected_admitted_trial_files"], 1)

    def test_missing_or_incomplete_admission_cannot_be_assumed_rejected(self):
        path = self.calibration / "rejected/admission.json"
        record = json.loads(path.read_text())
        record["_checkpoint"]["complete"] = False
        save_json(path, record)
        row = overlap.run(self.candidates, [self.calibration], ["ota"])["topologies"]["ota"]
        self.assertEqual(row["status"], "skipped_incomplete_checkpoint_set")
        self.assertEqual(row["admission_records"]["incomplete"], 1)
        self.assertEqual(row["rejected_candidates"], 0)
        path.unlink()
        row = overlap.run(self.candidates, [self.calibration], ["ota"])["topologies"]["ota"]
        self.assertEqual(row["admission_records"]["missing"], 1)
        self.assertFalse(row["checkpoint_set_complete"])

    def test_self_consistent_but_wrong_admission_identity_is_rejected(self):
        path = self.calibration / "rejected/admission.json"
        record = json.loads(path.read_text())
        identity = {**record["_checkpoint"]["identity"], "public_default_sha256": "wrong"}
        del record["_checkpoint"]
        save_checkpoint(path, identity, record)
        with self.assertRaisesRegex(ValueError, "Checkpoint identity changed"):
            overlap.run(self.candidates, [self.calibration], ["ota"])

    def test_self_consistent_false_rejection_flag_is_recomputed(self):
        path = self.calibration / "admitted/admission.json"
        record = json.loads(path.read_text())
        identity = record.pop("_checkpoint")["identity"]
        record["admitted"] = False
        save_checkpoint(path, identity, record)
        with self.assertRaisesRegex(ValueError, "Admission flag differs"):
            overlap.run(self.candidates, [self.calibration], ["ota"])


if __name__ == "__main__":
    unittest.main()
