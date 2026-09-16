from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark import report
from benchmark import select as selection_policy


PRIVATE_SENTINEL = "SECRET_REFERENCE_987654321"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def stats(successes=8, trials=10, median=7):
    return {"completed_trials": trials, "median_calls_censored_at_30": median,
            "success_at": {str(k): {"successes": successes, "trials": trials, "rate": successes / trials}
                           for k in (5, 10, 30)}}


def bundle_fixture(directory):
    bundle = directory / "bundle"
    entries = []
    for task_id, topology, family, split, level in (("one", "autockt_two_stage", "miller", "test", "easy"),
                                                   ("two", "released_ota", "compensation", "train", "hard")):
        task = {"id": task_id, "parameters": {"WIDTH": {"min": 1, "max": 10}},
                "initial_parameters": {"WIDTH": 1}, "max_evaluations": 30,
                "conditions": {"corner": "tt", "supply_v": 1.8, "temperature_c": 27},
                "ac": {"start_hz": 0.1, "points_per_decade": 100},
                "transient": {"low_v": 0.85, "high_v": 0.95, "settling_tolerance_v": 0.002, "minimum_hold_s": 2e-6}}
        if topology != "autockt_two_stage":
            task["transient"].update(low_v=0.3, high_v=0.5, settling_tolerance_v=0.02)
        save(bundle / "tasks" / (task_id + ".json"), task)
        reference_path = "private/" + task_id + "/reference.json"
        save(bundle / reference_path, {PRIVATE_SENTINEL: 123.456789})
        save(bundle / "private" / task_id / "baselines" / "private_trial.json", {"private": PRIVATE_SENTINEL})
        source_path = "sources/" + topology + ".json"
        source = {"paper": "Source paper", "source_url": "https://arxiv.org/pdf/2001.01808v2",
                  "method": "Figure inspected", "reference_design": {"parameters": {PRIVATE_SENTINEL: 123.456789}}}
        if topology != "autockt_two_stage":
            source = {"repository": "https://github.com/CODA-Team/AnalogGym", "commit": "abc123",
                      "release": {"paper": {"title": "Analog compensation paper", "doi": "10.1234/example",
                                             "review_level": "abstract_and_bibliography_only"},
                                  "review_limitations": ["Original full paper was unavailable."],
                                  "recommended_controls": [{"released_value": PRIVATE_SENTINEL}]}}
        save(bundle / source_path, source)
        entry = {"id": task_id, "path": "tasks/" + task_id + ".json", "reference_path": reference_path,
                 "topology": topology, "topology_family": family, "split": split, "difficulty": level,
                 "requirement_group": "group_" + task_id, "source_record": source_path,
                 "initial_failed_requirements": ["power_w"],
                 "baseline_summary": {method: stats() if level == "easy" else stats(2, 20, 30)
                                      for method in report.METHODS}}
        entry["sha256"] = report._digest(bundle / entry["path"])
        entry["reference_sha256"] = report._digest(bundle / reference_path)
        entries.append(entry)
    save(bundle / "index.json", {"collection": "test_bundle", "task_count": 2,
                                 "training_approved": False, "tasks": entries})
    return bundle


def verification_fixture(bundle, directory):
    index = json.loads((bundle / "index.json").read_text())
    inputs = {str(bundle / "index.json"): report._digest(bundle / "index.json")}
    rows = []
    for entry in index["tasks"]:
        for key in ("path", "reference_path"):
            inputs[str(bundle / entry[key])] = report._digest(bundle / entry[key])
        rows.append({"id": entry["id"], "status": "verified", "evaluations": {
            "initial": {"status": "ok", "success": False, "checks": {"power_w": False, "gain_db": True}},
            "reference": {"private": PRIVATE_SENTINEL}}})
    result = {"status": "verified", "scope": "fresh_simulation_and_static", "simulations_requested": True,
              "publication_ready": True, "index_path": str(bundle / "index.json"), "inputs_sha256": inputs,
              "task_count": len(rows), "tasks": rows, "errors": [], "unique_reference_count": 2,
              "computed_requirement_group_count": 2,
              "public_default_audit": {"status": "verified", "tasks_solved": [], "unresolved_tasks": [],
                                       "topologies": {"private_parameters": PRIVATE_SENTINEL},
                                       "task_outcomes": {row["id"]: {"status": "ok", "success": False} for row in rows}},
              "reference_diversity": {"passed": True, "minimum_required_distance": 0.025, "violations": [],
                                      "topologies": {"released_ota": {"reference_count": 1, "minimum_observed_distance": None,
                                                                      "nearest_references": {PRIVATE_SENTINEL: 0.25}}}},
              "reference_solution_coverage": {"scope": "Finite reference set only.", "topologies": {
                  "released_ota": {"task_count": 1, "greedy_cover_count": 1, "greedy_cover_reference_ids": [PRIVATE_SENTINEL],
                                   "maximum_tasks_solved_by_one_reference": 1, "uncovered_task_ids": [],
                                   "reference_coverage": [{"tasks_solved": [PRIVATE_SENTINEL], "unresolved_tasks": [],
                                                           "ambiguous_tasks": []}]}}}}
    path = directory / "verification.json"
    save(path, result)
    return path


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bundle = bundle_fixture(self.directory)

    def test_pending_report_uses_actual_counts_and_omits_private_values(self):
        with patch.object(report, "ROOT", self.directory):
            summary = report.write_report(self.bundle)
        self.assertEqual(summary["task_count"], 2)
        self.assertEqual(summary["difficulty_counts"], {"easy": 1, "medium": 0, "hard": 1})
        self.assertEqual(summary["split_counts"], {"train": 1, "validation": 0, "test": 1})
        self.assertEqual(summary["release_status"], "benchmark_candidates_pending_validation")
        self.assertEqual(summary["verification"]["status"], "pending")
        self.assertFalse(summary["training_approved"])
        self.assertEqual(summary["independent_expert_review"], "pending")
        self.assertEqual(summary["baselines"]["difficulty_summary_check"], "consistent")
        self.assertEqual(summary["baselines"]["methods"]["uniform_log"]["completed_trials"], 30)
        profiles = {row["source"]: row for row in summary["measurement_profiles"]}
        self.assertAlmostEqual(profiles["AutoCkt"]["settling_band_percent_of_step"], 2)
        self.assertAlmostEqual(profiles["AnalogGym"]["settling_band_percent_of_step"], 10)
        combined = "\n".join((self.bundle / name).read_text() for name in ("README.md", "TASK_CATALOG.md", "audit_summary.json"))
        self.assertNotIn(PRIVATE_SENTINEL, combined)
        self.assertNotIn("123.456789", combined)
        self.assertIn("2 new benchmark task candidates", combined)
        self.assertIn("abstract and bibliography only", combined)
        self.assertIn("Original full paper was unavailable", combined)
        self.assertIn("quiescent", combined.lower())
        self.assertIn("for_training=True", combined)
        self.assertIn("--expected-count 2 --simulate", combined)
        self.assertIn("[one](tasks/one.json)", combined)
        self.assertIn("power_w", combined)

    def test_bound_fresh_report_exposes_only_aggregate_coverage(self):
        path = verification_fixture(self.bundle, self.directory)
        summary = report.write_report(self.bundle, path)
        self.assertEqual(summary["release_status"], "automated_verification_passed")
        self.assertEqual(summary["verification"]["verified_task_count"], 2)
        self.assertEqual(summary["verification"]["public_default"]["valid_failed_tasks"], 2)
        coverage = summary["verification"]["reference_solution_coverage"]["topologies"]["released_ota"]
        self.assertEqual(coverage["greedy_cover_count"], 1)
        self.assertEqual(coverage["maximum_tasks_solved_by_one_reference"], 1)
        self.assertEqual(summary["tasks"][0]["starting_failure_evidence"], "fresh_verification")
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(summary))
        self.assertNotIn(PRIVATE_SENTINEL, (self.bundle / "README.md").read_text())
        self.assertFalse(summary["training_approved"])
        self.assertEqual(summary["independent_expert_review"], "pending")

    def test_stale_static_and_unresolved_audits_cannot_claim_verified_release(self):
        path = verification_fixture(self.bundle, self.directory)
        original = json.loads(path.read_text())
        for label in ("stale_index", "missing_reference_binding", "static", "unresolved_default", "missing_task"):
            with self.subTest(label=label):
                audit = deepcopy(original)
                if label == "stale_index":
                    audit["inputs_sha256"][audit["index_path"]] = "wrong"
                elif label == "missing_reference_binding":
                    del audit["inputs_sha256"][str(self.bundle / "private/one/reference.json")]
                elif label == "static":
                    audit.update(scope="static_only", simulations_requested=False, publication_ready=False)
                elif label == "unresolved_default":
                    audit["public_default_audit"].update(status="unresolved", unresolved_tasks=["one"])
                    audit["public_default_audit"]["task_outcomes"]["one"] = {"status": "unresolved", "success": None}
                else:
                    audit["tasks"].pop()
                save(path, audit)
                summary = report.build_summary(self.bundle, path)
                self.assertEqual(summary["release_status"], "benchmark_candidates_pending_validation")

    def test_edited_artifact_rejected_and_false_index_counts_reported(self):
        index_path = self.bundle / "index.json"
        index = json.loads(index_path.read_text())
        index["task_count"] = 250
        save(index_path, index)
        summary = report.build_summary(self.bundle)
        self.assertEqual(summary["task_count"], 2)
        self.assertIn("Declared task_count disagrees", " ".join(summary["warnings"]))
        save(self.bundle / "tasks/one.json", {"id": "modified"})
        with self.assertRaisesRegex(ValueError, "content hash disagrees"):
            report.build_summary(self.bundle)

    def test_family_leakage_underconfirmed_hard_and_old_pilot_are_visible(self):
        index_path = self.bundle / "index.json"
        index = json.loads(index_path.read_text())
        index["tasks"][1]["topology_family"] = "miller"
        index["tasks"][1]["baseline_summary"] = {method: stats(1, 10, 30) for method in report.METHODS}
        save(index_path, index)
        save(self.directory / "datasets/pilot_20260909/index.json", {"tasks": [index["tasks"][0]]})
        with patch.object(report, "ROOT", self.directory):
            summary = report.build_summary(self.bundle)
        self.assertFalse(summary["split_policy"]["consistent"])
        self.assertEqual(summary["baselines"]["difficulty_summary_check"], "inconsistent")
        self.assertEqual(summary["baselines"]["label_mismatches"][0]["from_summary"], "hard_candidate")
        self.assertEqual(summary["historical_pilot_exclusion"]["status"], "overlap")
        self.assertEqual(summary["historical_pilot_exclusion"]["pilot_task_count"], 1)
        self.assertEqual(summary["historical_pilot_exclusion"]["task_id_overlap_count"], 1)

    def test_four_method_reporting_uses_measured_engineering_results(self):
        index_path = self.bundle / "index.json"
        index = json.loads(index_path.read_text())
        for row in index["tasks"]:
            row["baseline_summary"]["engineering_sweep"] = stats(9, 10, 5)
        save(index_path, index)
        fourth = tuple(dict.fromkeys((*report.METHODS, "engineering_sweep")))
        with patch.object(report, "METHODS", fourth), patch.object(selection_policy, "METHODS", fourth):
            summary = report.write_report(self.bundle)
        self.assertEqual(summary["baselines"]["method_count"], 4)
        self.assertEqual(summary["baselines"]["methods"]["engineering_sweep"]["completed_trials"], 20)
        self.assertEqual(summary["baselines"]["label_mismatches"], [{"id": "two", "declared": "hard", "from_summary": "easy"}])
        readme = (self.bundle / "README.md").read_text()
        self.assertIn("4 declared search baselines", readme)
        self.assertIn("engineering_sweep", readme)
        self.assertIn("anchored ×0.5 and ×2", readme)
        self.assertNotIn("three declared", readme)
        del index["tasks"][0]["baseline_summary"]["engineering_sweep"]
        save(index_path, index)
        with patch.object(report, "METHODS", fourth), patch.object(selection_policy, "METHODS", fourth):
            summary = report.build_summary(self.bundle)
        self.assertEqual(summary["baselines"]["label_mismatches"][0]["from_summary"], "incomplete")

    def test_curation_report_uses_counts_without_exposing_answer_derived_control_names(self):
        index_path = self.bundle / "index.json"
        index = json.loads(index_path.read_text())
        for row in index["tasks"]:
            row.update(substantive_changed_control_count=1 if row["difficulty"] == "easy" else 3,
                       start_change_curation_passed=True,
                       start_variant="nearest" if row["difficulty"] == "easy" else "distant",
                       changed_controls=[PRIVATE_SENTINEL], substantive_changed_controls=[PRIVATE_SENTINEL])
        save(index_path, index)
        summary = report.write_report(self.bundle)
        self.assertEqual(summary["start_change_curation"]["status"], "metadata_consistent")
        self.assertEqual(summary["start_change_curation"]["per_difficulty"]["hard"]["reported_pass"], 1)
        combined = "\n".join((self.bundle / name).read_text() for name in ("README.md", "TASK_CATALOG.md", "audit_summary.json"))
        self.assertNotIn(PRIVATE_SENTINEL, combined)
        self.assertIn("exclusion rules", combined)


if __name__ == "__main__":
    unittest.main()
