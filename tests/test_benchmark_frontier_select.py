from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark import select as selection_policy
from benchmark.baselines import METHODS
from benchmark.calibrate import InputGuard, save_checkpoint
from benchmark.candidates import requirement_identity
from benchmark.frontier_select import PreparedPool, admission_failures, audit_selection, choose, select_files, start_changes
from benchmark.simulation import canonical_hash, save_json
from benchmark.verify_release import _hash


def method_stats(level):
    successes, count, median = {"easy": (8, 10, 5), "medium": (5, 10, 30), "hard": (2, 20, 30)}[level]
    return {"completed_trials": count, "median_calls_censored_at_30": median,
            "success_at": {str(k): {"successes": successes, "trials": count, "rate": successes / count}
                           for k in (5, 10, 30)}}


def example(index, level="easy", *, topology="ota", extra_support=(), value=None):
    reference = {name: 2.0 ** (index + 1) if value is None else value for name in ("C", "BIAS", "W")}
    task = {"id": "task_" + str(index), "circuit_directory": "circuits/" + topology, "subcircuit": topology,
            "conditions": {"supply_v": 1.8}, "parameters": {name: {"min": 1, "max": 65536, "unit": unit}
                                                           for name, unit in (("C", "F"), ("BIAS", "A"), ("W", "um"))},
            "ac": {}, "transient": {}, "constraints": {"gain_db": {"min": 60 + index}},
            "initial_parameters": {name: 1 for name in reference}, "max_evaluations": 30}
    key = canonical_hash(reference)
    support = sorted({key, *extra_support})
    group = canonical_hash(requirement_identity(task))
    signature = canonical_hash(support)
    target = {"topology": topology, "key": key, "parameters": reference, "constraints": task["constraints"],
              "requirement_group": group, "empirical_solution_signature": signature,
              "empirical_feasible_design_keys": support, "empirical_pool_feasible_count": len(support),
              "empirical_pool_valid_count": 100}
    candidate = {"id": task["id"], "topology": topology, "topology_family": "family_" + topology,
                 "task": task, "reference": reference, "target_key": key, "requirement_group": group,
                 "empirical_solution_signature": signature,
                 "start_variant": {"easy": "nearest", "medium": "intermediate", "hard": "distant"}[level]}
    def measured(gain, success):
        return {"status": "ok", "success": success, "metrics": {"gain_db": gain}, "independent_metrics": {"gain_db": gain}}
    admission = {"admitted": True, "public_seed_status": "valid_failure", "effective_task": deepcopy(task),
                 "rounded_reference_parameters": deepcopy(reference), "evaluations": {
                     "initial": measured(59 + index, False), "reference": measured(61 + index, True),
                     "public_seed": measured(58 + index, False)}}
    row = {"candidate": candidate, "observation": {"id": task["id"], "admitted": True,
                                                     "methods": {method: method_stats(level) for method in METHODS}},
           "admission": admission, "level": level, "calibration_directory": "runs/confirmation"}
    return target, row


class FrontierSelectionTests(unittest.TestCase):
    def test_exact_quotas_use_confirmed_labels_and_keep_observations_unchanged(self):
        pairs = [example(i, level) for i, level in enumerate(("easy", "easy", "medium", "medium", "hard", "hard"))]
        targets, observations = map(list, zip(*pairs))
        before = deepcopy(observations)
        selected, report = choose(observations, PreparedPool(targets), {"easy": 2, "medium": 2, "hard": 2})
        self.assertTrue(report["complete"])
        self.assertEqual(report["selected_levels"], {"easy": 2, "medium": 2, "hard": 2})
        self.assertEqual(report["unique_reference_count"], 6)
        self.assertEqual(report["unique_requirement_group_count"], 6)
        self.assertGreaterEqual(report["minimum_observed_reference_distance_by_topology"]["ota"], 0.025)
        self.assertEqual(report["coverage"]["topologies"]["ota"]["max_single_design_coverage"], 1)
        self.assertEqual(report["coverage"]["topologies"]["ota"]["greedy_cover_count"], 6)
        self.assertEqual(report["fresh_release_audit"], "pending")
        self.assertFalse(report["training_approved"])
        self.assertEqual(observations, before)
        self.assertEqual([row["candidate"]["id"] for row in selected],
                         [row["candidate"]["id"] for row in choose(observations, PreparedPool(targets),
                          {"easy": 2, "medium": 2, "hard": 2})[0]])

    def test_selected_subset_uses_unselected_witnesses_and_its_own_denominator(self):
        pairs = [example(i, extra_support=("outside_reference_pool",) if i < 3 else ()) for i in range(10)]
        targets, rows = map(list, zip(*pairs))
        pool = PreparedPool(targets)
        full = pool.coverage("ota", [target["key"] for target in targets])
        subset = audit_selection(rows[:3], pool)
        self.assertEqual(full["max_single_design_fraction"], 0.3)
        self.assertEqual(subset["topologies"]["ota"]["max_single_design_fraction"], 1)
        self.assertFalse(subset["passed"])
        self.assertEqual(subset["topologies"]["ota"]["greedy_cover_count"], 1)
        self.assertIn("Later-completed discovery measurements are not included", subset["scope"])

    def test_greedy_floor_is_separate_from_cap_and_never_claimed_as_optimal(self):
        pairs = [example(i, extra_support=("group_A" if i < 5 else "group_B",)) for i in range(10)]
        targets, rows = map(list, zip(*pairs))
        _, report = choose(rows, PreparedPool(targets), {"easy": 10, "medium": 0, "hard": 0})
        self.assertFalse(report["complete"])
        coverage = report["coverage"]["topologies"]["ota"]
        self.assertEqual(coverage["max_single_design_fraction"], 0.5)
        self.assertEqual(coverage["greedy_cover_count"], 2)
        self.assertEqual(coverage["violations"], ["greedy_cover_below_requested_construction_floor"])
        self.assertIn("does not prove", report["coverage"]["scope"])
        self.assertTrue(report["search"]["partial_is_not_infeasibility_proof"])

    def test_bounded_swap_can_fix_overlap_without_changing_difficulty_quota(self):
        # Each edge is an extra measured vector that solves its endpoint tasks.
        # This graph has a four-task independent set that the first greedy pass misses.
        edges = [(0, 1), (0, 5), (0, 6), (1, 7), (2, 3), (2, 6),
                 (3, 5), (4, 5), (4, 7), (5, 7), (6, 7)]
        supports = [[] for _ in range(8)]
        for first, second in edges:
            key = f"edge_{first}_{second}"
            supports[first].append(key)
            supports[second].append(key)
        targets, rows = map(list, zip(*(example(i, extra_support=supports[i]) for i in range(8))))
        pool = PreparedPool(targets)
        _, before = choose(rows, pool, {"easy": 4, "medium": 0, "hard": 0}, substantive_tasks=4, maximum_repair_steps=0)
        self.assertFalse(before["complete"])
        selected, after = choose(rows, pool, {"easy": 4, "medium": 0, "hard": 0}, substantive_tasks=4)
        self.assertTrue(after["complete"])
        self.assertEqual(after["selected_levels"], {"easy": 4, "medium": 0, "hard": 0})
        self.assertEqual(len(after["repairs"]), 1)
        self.assertTrue(all(row["level"] == "easy" for row in selected))
        self.assertEqual(after["coverage"]["topologies"]["ota"]["greedy_cover_count"], 4)

    def test_clear_failures_and_confirmation_thresholds_are_required(self):
        targets, rows = map(list, zip(*(example(i, "hard") for i in range(3))))
        near = rows[0]["admission"]["evaluations"]["initial"]
        near["metrics"]["gain_db"] = 59.9
        near["independent_metrics"]["gain_db"] = 59.9
        self.assertEqual(admission_failures(rows[0]["candidate"], rows[0]["admission"])[1], "no_common_clear_initial_failure")
        native = rows[1]["admission"]["evaluations"]["initial"]
        native["independent_metrics"]["gain_db"] = 60.9
        self.assertEqual(admission_failures(rows[1]["candidate"], rows[1]["admission"])[1], "no_common_clear_initial_failure")
        for stats in rows[2]["observation"]["methods"].values():
            stats["completed_trials"] = 10
        rows[2]["level"] = "hard"  # A supplied hint cannot override the measured policy.
        selected, report = choose(rows, PreparedPool(targets), {"easy": 0, "medium": 0, "hard": 3})
        self.assertEqual(selected, [])
        self.assertEqual(report["rejected_admissions"]["no_common_clear_initial_failure"], 2)
        self.assertEqual(report["rejected_admissions"]["unconfirmed_difficulty_hard_candidate"], 1)
        self.assertEqual(report["shortfalls"]["hard"], 3)

    def test_fourth_method_can_make_hard_hint_easy_and_missing_evidence_incomplete(self):
        targets, rows = map(list, zip(*(example(i, "hard") for i in range(2))))
        fourth = tuple(dict.fromkeys((*METHODS, "engineering_sweep")))
        for row in rows:
            row["observation"]["methods"]["engineering_sweep"] = method_stats("easy")
        with patch.object(selection_policy, "METHODS", fourth):
            selected, report = choose(rows, PreparedPool(targets), {"easy": 2, "medium": 0, "hard": 0})
            self.assertTrue(report["complete"])
            self.assertTrue(all(row["level"] == "easy" for row in selected))
            self.assertEqual(report["declared_baselines"], list(fourth))
            del rows[0]["observation"]["methods"]["engineering_sweep"]
            _, partial = choose(rows, PreparedPool(targets), {"easy": 2, "medium": 0, "hard": 0})
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["rejected_admissions"]["unconfirmed_difficulty_incomplete"], 1)

    def test_curation_filters_reject_shortcuts_without_relabeling(self):
        targets, rows = map(list, zip(*(example(i, "medium" if i < 2 else "hard") for i in range(6))))
        for index, row in enumerate(rows):
            candidate = row["candidate"]
            if index in (0, 1, 2):
                candidate["task"]["initial_parameters"]["W"] = candidate["reference"]["W"]
            if index == 0:
                candidate["task"]["initial_parameters"]["BIAS"] = candidate["reference"]["BIAS"]
            if index == 3:
                candidate["start_variant"] = "nearest"
            if index == 4:
                candidate["task"]["initial_parameters"]["W"] = candidate["reference"]["W"] * 1.00000001
            row["admission"]["effective_task"] = deepcopy(candidate["task"])
        selected, report = choose(rows, PreparedPool(targets), {"easy": 0, "medium": 1, "hard": 1})
        self.assertTrue(report["complete"])
        self.assertEqual({row["candidate"]["id"] for row in selected}, {"task_1", "task_5"})
        self.assertEqual(report["rejected_admissions"]["medium_fewer_than_two_substantive_controls"], 1)
        self.assertEqual(report["rejected_admissions"]["hard_requires_distant_variant"], 1)
        self.assertEqual(report["rejected_admissions"]["hard_fewer_than_three_substantive_controls"], 2)
        self.assertEqual(start_changes(rows[4]["candidate"])["changed_control_count"], 3)
        self.assertEqual(start_changes(rows[4]["candidate"])["substantive_changed_control_count"], 2)

    def test_substantive_change_uses_log_linear_and_integer_coordinates(self):
        candidate = {"reference": {"log": 10, "linear": 0, "integer": 1},
                     "task": {"parameters": {"log": {"min": 1, "max": 100},
                                             "linear": {"min": -10, "max": 10},
                                             "integer": {"min": 1, "max": 1000, "integer": True}},
                              "initial_parameters": {"log": 10 * 100 ** 0.0249, "linear": 0.502, "integer": 2}}}
        changes = start_changes(candidate)
        self.assertEqual(changes["changed_control_count"], 3)
        self.assertEqual(changes["substantive_changed_controls"], ["integer", "linear"])
        candidate["task"]["initial_parameters"]["log"] = 10 * 100 ** 0.0251
        self.assertEqual(start_changes(candidate)["substantive_changed_control_count"], 3)

    def test_duplicate_reference_group_and_close_vectors_cannot_fill_quota(self):
        first, a = example(0)
        second, b = example(1, value=first["parameters"]["C"] * 1.001)
        third, c = example(2)
        duplicate = deepcopy(a)
        duplicate["candidate"]["id"] = "different_start_same_requirements"
        duplicate["candidate"]["task"]["id"] = duplicate["candidate"]["id"]
        duplicate["admission"]["effective_task"] = deepcopy(duplicate["candidate"]["task"])
        selected, report = choose([a, b, c, duplicate], PreparedPool([first, second, third]),
                                   {"easy": 4, "medium": 0, "hard": 0})
        self.assertFalse(report["complete"])
        self.assertEqual(len(selected), 2)
        self.assertEqual(report["unique_reference_count"], 2)
        self.assertEqual(report["unique_requirement_group_count"], 2)
        self.assertEqual(report["shortfalls"]["easy"], 2)

    def test_prepared_support_hashes_and_curation_bitmasks_are_crosschecked(self):
        first, _ = example(0)
        second, _ = example(1)
        targets = [first, second]
        for target in targets:
            target["empirical_pool_valid_count"] = 2
        curation = {"topologies": [{"topology": "ota", "coverage": {
            "target_keys_in_bit_order": [second["key"], first["key"]], "characterized_design_count": 2,
            "coverage_bitmasks_hex": {first["key"]: "0x2", second["key"]: "0x1"}}}]}
        pool = PreparedPool(targets, curation)
        self.assertIn("crosschecked", pool.evidence)
        altered = deepcopy(targets)
        altered[0]["empirical_solution_signature"] = "bad"
        with self.assertRaisesRegex(ValueError, "signature"):
            PreparedPool(altered)
        curation["topologies"][0]["coverage"]["coverage_bitmasks_hex"][first["key"]] = "0x3"
        with self.assertRaisesRegex(ValueError, "disagree"):
            PreparedPool(targets, curation)

    def test_file_selection_preserves_all_stage_observations_and_final_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            targets, rows = map(list, zip(*(example(i) for i in range(2))))
            entries = []
            for row in rows:
                candidate = row["candidate"]
                relative = "candidates/" + candidate["id"] + ".json"
                save_json(root / "candidates" / relative, candidate)
                entries.append({"id": candidate["id"], "path": relative})
            save_json(root / "candidates/index.json", {"tasks": entries})
            save_json(root / "targets.json", {"targets": targets})
            stages = [root / "screen", root / "final_confirmation"]
            for stage in stages:
                observations = []
                for row in rows:
                    observed = deepcopy(row["observation"])
                    if stage.name == "screen":
                        for stats in observed["methods"].values():
                            stats["completed_trials"] = 1
                    observations.append(observed)
                    save_checkpoint(stage / row["candidate"]["id"] / "admission.json", {"stage": stage.name}, row["admission"])
                save_json(stage / "summary.json", {"tasks": observations})
            tracked = [*root.glob("candidates/candidates/*.json"), *root.glob("*/summary.json"), *root.glob("*/*/admission.json")]
            before = {path: path.read_bytes() for path in tracked}
            result = select_files(root / "candidates", stages, root / "targets.json", root / "selection.json",
                                  counts={"easy": 2, "medium": 0, "hard": 0})
            self.assertEqual(result["status"], "selection_complete")
            self.assertTrue(all(row["calibration_directory"] == str(stages[-1]) for row in result["tasks"]))
            self.assertEqual(len(result["calibration_observations"]["task_0"]), 2)
            self.assertEqual(result["calibration_observations"]["task_0"][0]["observed_level"], "incomplete")
            self.assertEqual({path: path.read_bytes() for path in tracked}, before)
            self.assertEqual(json.loads((root / "selection.json").read_text())["ids"], result["ids"])


def full_curation(targets, zero_keys=()):
    grouped = {}
    for target in targets:
        grouped.setdefault(target["topology"], []).append(target)
    rows = []
    for topology, same in grouped.items():
        masks = {key: 0 for key in zero_keys}
        for bit, target in enumerate(same):
            for witness in target["empirical_feasible_design_keys"]:
                masks[witness] = masks.get(witness, 0) | (1 << bit)
        for target in same:
            target["empirical_pool_valid_count"] = len(masks)
        rows.append({"topology": topology, "coverage": {
            "target_keys_in_bit_order": [target["key"] for target in same],
            "characterized_design_count": len(masks),
            "coverage_bitmasks_hex": {key: hex(mask) for key, mask in masks.items()}}})
    return {"topologies": rows}


def trajectory_report(targets, witnesses):
    """Witness triples contain the exact vector, agreed mask and either-path mask."""
    topology = targets[0]["topology"]
    return {"schema_version": 1, "topologies": {topology: {
        "status": "diagnostic_complete", "checkpoint_set_complete": True,
        "pending_admission_records": 0, "missing_trial_files": 0,
        "target_keys_in_bit_order": [target["key"] for target in targets],
        "unique_valid_parameter_vectors": len(witnesses),
        "trajectory_agreed_coverage_bitmasks_hex": {_hash(vector): hex(first) for vector, first, second in witnesses},
        "trajectory_either_path_coverage_bitmasks_hex": {_hash(vector): hex(second) for vector, first, second in witnesses},
        "trajectory_witness_original_hashes": {_hash(vector): [canonical_hash(vector)] for vector, first, second in witnesses}}}}


class FrontierTrajectoryCoverageTests(unittest.TestCase):
    def test_either_path_union_preserves_original_support_and_zero_witnesses(self):
        targets, rows = map(list, zip(*(example(i) for i in range(3))))
        old_zero, new_zero = {"unused": 2.0}, {"new_unused": 7.5}
        curation = full_curation(targets, [canonical_hash(old_zero)])
        pool = PreparedPool(targets, curation)
        original_support = deepcopy(pool.support)
        original_keys = deepcopy(pool.original_witness_keys)
        # Reverse bit order and retain zero masks in all three diagnostic maps.
        report = trajectory_report(targets[::-1], [(targets[0]["parameters"], 4, 6), (old_zero, 0, 0), (new_zero, 0, 0)])
        pool.add_trajectory_coverage([report], {"ota": {target["key"] for target in targets}})
        self.assertEqual(pool.support, original_support)
        self.assertEqual(pool.original_witness_keys, original_keys)
        self.assertEqual(pool.original_pool_counts["ota"], 4)
        self.assertEqual(pool.pool_counts["ota"], 5)
        self.assertEqual(pool.masks["ota"][_hash(old_zero)], 0)
        self.assertEqual(pool.masks["ota"][_hash(new_zero)], 0)
        self.assertIn(_hash(targets[0]["parameters"]), pool.coverage_support[("ota", targets[1]["key"])])
        selected, audit = choose(rows, pool, {"easy": 3, "medium": 0, "hard": 0})
        self.assertEqual(len(selected), 3)
        self.assertEqual(audit["selected_levels"], {"easy": 3, "medium": 0, "hard": 0})
        self.assertFalse(audit["complete"])
        self.assertEqual(audit["coverage"]["topologies"]["ota"]["max_single_design_coverage"], 2)
        self.assertIn("either-path potential coverage", audit["coverage"]["scope"])
        self.assertIn("does not prove", audit["coverage"]["scope"])

    def test_repeated_reports_merge_exact_witness_coverage_and_not_nearby_vectors(self):
        targets, _ = map(list, zip(*(example(i) for i in range(3))))
        pool = PreparedPool(targets, full_curation(targets))
        exact, nearby = {"C": 12.3456789}, {"C": 12.3456790}
        first = trajectory_report(targets, [(exact, 1, 1), (nearby, 0, 0)])
        second = trajectory_report(targets[::-1], [(exact, 1, 1)])
        pool.add_trajectory_coverage([first, second], {"ota": {target["key"] for target in targets}})
        self.assertEqual(pool.masks["ota"][_hash(exact)], 5)
        self.assertEqual(pool.masks["ota"][_hash(nearby)], 0)
        self.assertEqual(pool.pool_counts["ota"], 5)

    def test_greedy_selection_uses_augmented_overlap_without_changing_labels(self):
        targets, rows = map(list, zip(*(example(i) for i in range(3))))
        curation = full_curation(targets)
        counts = {"easy": 2, "medium": 0, "hard": 0}
        original, _ = choose(rows, PreparedPool(targets, curation), counts)
        self.assertEqual({row["candidate"]["id"] for row in original}, {"task_0", "task_2"})
        pool = PreparedPool(targets, curation)
        report = trajectory_report(targets, [({"C": 42.42}, 0, 5)])
        pool.add_trajectory_coverage([report], {"ota": {target["key"] for target in targets}})
        selected, audit = choose(rows, pool, counts)
        self.assertEqual({row["candidate"]["id"] for row in selected}, {"task_0", "task_1"})
        self.assertTrue(audit["complete"])
        self.assertEqual(audit["selected_levels"], counts)

    def test_separate_reports_can_cover_distinct_used_topologies(self):
        first, a = example(0, topology="a")
        second, b = example(1, topology="b")
        pool = PreparedPool([first, second], full_curation([first, second]))
        reports = [trajectory_report([target], [(target["parameters"], 1, 1)]) for target in (first, second)]
        pool.add_trajectory_coverage(reports, {"a": {first["key"]}, "b": {second["key"]}})
        self.assertEqual(pool.pool_counts, {"a": 1, "b": 1})

    def test_invalid_masks_missing_targets_aliases_and_partial_reports_fail_before_union(self):
        targets, _ = map(list, zip(*(example(i) for i in range(2))))
        curation = full_curation(targets)
        valid = trajectory_report(targets, [(targets[0]["parameters"], 1, 1)])
        key = _hash(targets[0]["parameters"])
        mutations = [
            lambda row: row.update(status="skipped_incomplete_checkpoint_set"),
            lambda row: row.update(checkpoint_set_complete=False),
            lambda row: row.update(pending_admission_records=1),
            lambda row: row.update(missing_trial_files=1),
            lambda row: row.update(target_keys_in_bit_order=[targets[0]["key"]] * 2),
            lambda row: row.update(target_keys_in_bit_order=[targets[0]["key"]]),
            lambda row: row.update(target_keys_in_bit_order=[targets[0]["key"], "unknown"]),
            lambda row: row["trajectory_either_path_coverage_bitmasks_hex"].update({key: "0x4"}),
            lambda row: row["trajectory_either_path_coverage_bitmasks_hex"].update({key: "-0x1"}),
            lambda row: row["trajectory_either_path_coverage_bitmasks_hex"].update({key: 1}),
            lambda row: row["trajectory_either_path_coverage_bitmasks_hex"].update({key: "0x0"}),
            lambda row: row["trajectory_agreed_coverage_bitmasks_hex"].clear(),
            lambda row: row["trajectory_witness_original_hashes"].clear(),
            lambda row: row["trajectory_witness_original_hashes"].update({key: ["not_a_hash"]}),
            lambda row: row.update(unique_valid_parameter_vectors=0),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                pool = PreparedPool(targets, curation)
                before = deepcopy(pool.masks)
                report = deepcopy(valid)
                mutate(report["topologies"]["ota"])
                with self.assertRaises(ValueError):
                    pool.add_trajectory_coverage([report], {"ota": {target["key"] for target in targets}})
                self.assertEqual(pool.masks, before)
        with self.assertRaisesRegex(ValueError, "missing"):
            PreparedPool(targets, curation).add_trajectory_coverage([valid], {"other": {"target"}})
        with self.assertRaisesRegex(ValueError, "curation"):
            PreparedPool(targets).add_trajectory_coverage([valid], {"ota": {target["key"] for target in targets}})

    def test_original_curation_validation_cannot_be_bypassed_by_trajectory_evidence(self):
        targets, _ = map(list, zip(*(example(i) for i in range(2))))
        curation = full_curation(targets)
        curation["topologies"][0]["coverage"]["coverage_bitmasks_hex"][targets[0]["key"]] = "0x3"
        with self.assertRaisesRegex(ValueError, "disagree"):
            PreparedPool(targets, curation)

    def file_fixture(self, root):
        targets, rows = map(list, zip(*(example(i) for i in range(2))))
        curation = full_curation(targets)
        entries, tracked = [], []
        for row in rows:
            candidate = row["candidate"]
            path = root / "candidates/candidates" / (candidate["id"] + ".json")
            save_json(path, candidate)
            tracked.append(path)
            entries.append({"id": candidate["id"], "path": str(path.relative_to(root / "candidates"))})
            admission = root / "confirmation" / candidate["id"] / "admission.json"
            save_checkpoint(admission, {"stage": "confirmation"}, row["admission"])
            tracked.append(admission)
        save_json(root / "candidates/index.json", {"tasks": entries})
        save_json(root / "candidates/targets.json", {"targets": targets})
        save_json(root / "prepared.json", {"targets": targets})
        save_json(root / "curation.json", curation)
        save_json(root / "confirmation/summary.json", {"tasks": [row["observation"] for row in rows]})
        report = trajectory_report(targets, [(target["parameters"], 1 << bit, 1 << bit) for bit, target in enumerate(targets)])
        tracked.extend([root / "candidates/index.json", root / "candidates/targets.json", root / "curation.json"])
        report["inputs_sha256"] = InputGuard(tracked).hashes
        save_json(root / "trajectory.json", report)
        return report

    def select_fixture(self, root):
        return select_files(root / "candidates", [root / "confirmation"], root / "prepared.json", root / "selection.json",
                            curation_report=root / "curation.json", trajectory_coverage=[root / "trajectory.json"],
                            counts={"easy": 2, "medium": 0, "hard": 0})

    def test_file_selection_binds_diagnostic_and_inputs_without_relabeling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = self.file_fixture(root)
            result = self.select_fixture(root)
            self.assertEqual(result["status"], "selection_complete")
            self.assertTrue(all(row["difficulty"] == "easy" for row in result["tasks"]))
            source = result["trajectory_coverage_sources"][0]
            self.assertEqual(source["inputs_sha256"], report["inputs_sha256"])
            self.assertEqual(source["sha256"], InputGuard([root / "trajectory.json"]).hashes[str((root / "trajectory.json").resolve())])
            self.assertTrue(set(report["inputs_sha256"]) <= set(result["inputs_sha256"]))

    def test_stale_diagnostic_wrong_index_and_changed_target_fail_before_output(self):
        for mutation in ("checkpoint", "index_binding", "target_definition", "duplicate_json_key"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report = self.file_fixture(root)
                if mutation == "checkpoint":
                    (root / "confirmation/task_0/admission.json").write_text("{}")
                elif mutation == "index_binding":
                    del report["inputs_sha256"][str((root / "candidates/index.json").resolve())]
                    save_json(root / "trajectory.json", report)
                elif mutation == "target_definition":
                    path = root / "candidates/targets.json"
                    frozen = json.loads(path.read_text())
                    frozen["targets"][0]["constraints"] = {"gain_db": {"min": 999}}
                    save_json(path, frozen)
                    report["inputs_sha256"].update(InputGuard([path]).hashes)
                    save_json(root / "trajectory.json", report)
                else:
                    (root / "trajectory.json").write_text('{"schema_version":1,"schema_version":1}')
                with self.assertRaises(ValueError):
                    self.select_fixture(root)
                self.assertFalse((root / "selection.json").exists())


if __name__ == "__main__":
    unittest.main()
