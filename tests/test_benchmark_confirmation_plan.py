"""Provisional queue allocation and one-seed checkpoint integration contracts."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.metrics import score
from analog_design.simulator import ROOT, digest
from benchmark.baselines import METHODS, run_search
from benchmark.calibrate import (CODE_FILES, PHYSICAL_FIELDS, admission_decision,
                                 make_summary, save_checkpoint, summarize_trials)
from benchmark.candidates import requirement_identity
from benchmark.confirmation_plan import plan, plan_files
from benchmark.explore import rounded_parameters
from benchmark.simulation import canonical_hash, save_json


def example(index, calls=(5, None, None, None), *, changed=3, variant="intermediate", topology="ota"):
    reference = {name: float(2 ** (index + 2)) for name in ("C", "I", "W")}
    initial = {name: value / 2 if position < changed else value for position, (name, value) in enumerate(reference.items())}
    task = {"id": f"screen_{index}", "circuit_directory": "circuits/" + topology, "subcircuit": topology,
            "conditions": {"supply_v": 1.8}, "parameters": {name: {"min": 1.0, "max": 4096.0, "unit": "F"} for name in reference},
            "ac": {}, "transient": {}, "constraints": {"gain_db": {"min": 60 + index}},
            "initial_parameters": initial, "max_evaluations": 30}
    candidate = {"id": task["id"], "topology": topology, "topology_family": "family_" + topology,
                 "task": task, "reference": reference, "start_variant": variant,
                 "requirement_group": canonical_hash(requirement_identity(task))}

    def measurement(gain):
        metrics = {"gain_db": gain}
        return {"status": "ok", "metrics": metrics, "independent_metrics": deepcopy(metrics), **score(metrics, task["constraints"])}

    admission = {"id": task["id"], "admitted": True, "public_seed_status": "valid_failure",
                 "effective_task": deepcopy(task), "rounded_reference_parameters": deepcopy(reference),
                 "evaluations": {"initial": measurement(59 + index), "reference": measurement(61 + index),
                                 "public_seed": measurement(58 + index)}}
    stats = {method: summarize_trials([{"success": call is not None, "first_success_evaluation": call,
                                       "evaluations_used": call or 30}]) for method, call in zip(METHODS, calls)}
    return {"candidate": candidate, "admission": admission,
            "observation": {"id": task["id"], "admitted": True, "methods": stats},
            "calibration_directory": "runs/synthetic_screen", "screening_seed": 0}


def variant_of(row, name, calls=(4, None, None, None)):
    row = deepcopy(row)
    for location in (row["candidate"], row["candidate"]["task"], row["admission"],
                     row["admission"]["effective_task"], row["observation"]):
        location["id"] = name
    row["observation"]["methods"] = example(0, calls)["observation"]["methods"]
    return row


def set_reference(row, value):
    values = {name: value for name in row["candidate"]["reference"]}
    row["candidate"]["reference"] = values
    row["admission"]["rounded_reference_parameters"] = deepcopy(values)
    row["candidate"]["task"]["initial_parameters"] = {name: value / 2 for name in values}
    row["admission"]["effective_task"] = deepcopy(row["candidate"]["task"])


class ConfirmationQueueTests(unittest.TestCase):
    def test_three_queues_use_observations_without_assigning_final_labels(self):
        rows = [example(0, changed=1, variant="nearest"), example(1, (15, 24, None, None)),
                example(2, (None,) * 4, variant="distant")]
        before = deepcopy(rows)
        result = plan(rows, {"fast": 1, "medium_priority": 1, "hard_priority": 1})
        self.assertTrue(result["report"]["complete"])
        self.assertEqual([row["queue"] for row in result["tasks"]], ["hard_priority", "medium_priority", "fast"])
        self.assertEqual(len(set(result["ids"])), 3)
        self.assertEqual(result["difficulty_status"], "unassigned")
        self.assertEqual(result["confirmation_status"], "not_run")
        self.assertFalse(result["training_approved"])
        self.assertTrue(all("difficulty" not in row and "level" not in row for row in result["tasks"]))
        self.assertEqual(rows, before)

    def test_medium_and_hard_curation_ignore_misleading_construction_hints(self):
        rows = [example(0, (15, None, None, None), changed=1),
                example(1, (None,) * 4, changed=3, variant="nearest"),
                example(2, (None,) * 4, changed=2, variant="distant")]
        for row in rows:
            row["candidate"]["proposed_difficulty"] = "hard"
            row["candidate"]["empirical_pool_feasible_count"] = 1
        result = plan(rows, {"fast": 0, "medium_priority": 1, "hard_priority": 1})
        self.assertEqual(result["ids"], [])
        self.assertEqual(result["report"]["shortfalls"], {"fast": 0, "medium_priority": 1, "hard_priority": 1})
        self.assertEqual(len(result["reserves"]), 3)

    def test_preferred_medium_and_all_failure_hard_precede_fallbacks(self):
        rows = [example(0, (4, 25, None, None)), example(1, (18, None, None, None)),
                example(2, (28, None, None, None), variant="distant"),
                example(3, (None,) * 4, variant="distant")]
        result = plan(rows, {"fast": 0, "medium_priority": 1, "hard_priority": 1})
        self.assertEqual(result["ids"], ["screen_3", "screen_1"])
        self.assertEqual(result["report"]["fallback_selected"]["medium_priority"], 0)
        fallback = plan([rows[0]], {"fast": 0, "medium_priority": 1, "hard_priority": 0})
        self.assertEqual(fallback["report"]["fallback_selected"]["medium_priority"], 1)
        self.assertEqual(result["reserve_ids_by_queue"]["medium_priority"][0], "screen_2")

    def test_groups_references_and_distance_reserve_only_one_compatible_task(self):
        first = example(0, (18, None, None, None))
        alias = deepcopy(first)
        for location in (alias["candidate"], alias["candidate"]["task"], alias["admission"],
                         alias["admission"]["effective_task"], alias["observation"]):
            location["id"] = "screen_alias"
        alias["observation"]["methods"] = example(1)["observation"]["methods"]
        repeated_reference = example(2)
        repeated_reference["candidate"]["reference"] = deepcopy(first["candidate"]["reference"])
        repeated_reference["admission"]["rounded_reference_parameters"] = deepcopy(first["candidate"]["reference"])
        repeated_reference["candidate"]["task"]["initial_parameters"] = {name: 2.0 for name in ("C", "I", "W")}
        repeated_reference["admission"]["effective_task"] = deepcopy(repeated_reference["candidate"]["task"])
        near = deepcopy(repeated_reference)
        for location in (near["candidate"], near["candidate"]["task"], near["admission"],
                         near["admission"]["effective_task"], near["observation"]):
            location["id"] = "screen_near"
        near["candidate"]["reference"] = {name: 4.001 for name in ("C", "I", "W")}
        near["admission"]["rounded_reference_parameters"] = deepcopy(near["candidate"]["reference"])
        result = plan([first, alias, repeated_reference, near], {"fast": 1, "medium_priority": 1, "hard_priority": 0})
        self.assertEqual(result["ids"], ["screen_0"])
        self.assertEqual({row["reservation_conflict"] for row in result["reserves"]},
                         {"requirement_group_reserved", "reference_reserved", "reference_too_close"})

    def test_topologies_are_balanced_and_order_is_deterministic(self):
        rows = [example(i, changed=1, topology="a" if i < 3 else "b") for i in range(6)]
        counts = {"fast": 4, "medium_priority": 0, "hard_priority": 0}
        result = plan(rows, counts)
        self.assertEqual(result["report"]["selected_topologies"], {"a": 2, "b": 2})
        self.assertEqual(result, plan(list(reversed(rows)), counts))

    def test_missing_methods_extra_seeds_and_identity_disagreement_are_rejected(self):
        for mutation in (lambda row: row["observation"]["methods"].pop(METHODS[0]),
                         lambda row: row["observation"]["methods"][METHODS[0]].update(completed_trials=2),
                         lambda row: row["admission"]["effective_task"].update(id="wrong")):
            row = example(0)
            mutation(row)
            with self.assertRaises(ValueError):
                plan([row], {"fast": 1, "medium_priority": 0, "hard_priority": 0})
        with self.assertRaisesRegex(ValueError, "exactly one input run"):
            plan([example(0), example(0)])

    def test_failed_recomputed_admission_stays_out_of_confirmation(self):
        row = example(0)
        row["admission"]["evaluations"]["reference"]["success"] = False
        result = plan([row], {"fast": 1, "medium_priority": 0, "hard_priority": 0})
        self.assertEqual(result["ids"], [])
        self.assertEqual(result["report"]["admission_rejections"][0]["reason"], "invalid_reference")


class ConfirmationReserveRepairTests(unittest.TestCase):
    counts = {"fast": 1, "medium_priority": 1, "hard_priority": 0}

    def test_direct_reserve_reassignment_fills_actual_greedy_group_shortfall(self):
        medium = example(0, (12, None, None, None))
        fast = variant_of(medium, "same_group_fast")
        reserve = example(1, (20, None, None, None))
        rows = [medium, fast, reserve]
        before = deepcopy(rows)
        result = plan(rows, self.counts)
        self.assertTrue(result["report"]["complete"])
        self.assertEqual(result["report"]["greedy_selected_queues"], {"fast": 0, "medium_priority": 1, "hard_priority": 0})
        self.assertEqual(result["report"]["selected_queues"], self.counts)
        self.assertEqual(set(result["ids"]), {"same_group_fast", "screen_1"})
        self.assertEqual(result["report"]["selected_unique_requirement_groups"], 2)
        self.assertEqual(result["report"]["selected_unique_references"], 2)
        repair, = result["report"]["reserve_reassignments"]
        self.assertEqual(repair["removed_task_id"], "screen_0")
        self.assertEqual(repair["reassigned_task_id"], "same_group_fast")
        self.assertEqual(repair["replacement_task_id"], "screen_1")
        self.assertTrue(repair["same_topology_replacement"])
        self.assertLessEqual(repair["replacement_priority_tier"], repair["displaced_priority_tier"])
        self.assertTrue(repair["combined_identity_distance_topology_checks_passed"])
        self.assertEqual(result["difficulty_status"], "unassigned")
        self.assertTrue(all("difficulty" not in row for row in result["tasks"]))
        self.assertEqual(rows, before)
        self.assertEqual(result, plan(list(reversed(rows)), self.counts))

    def test_same_topology_is_preferred_but_combined_cap_still_applies(self):
        medium = example(0, (12, None, None, None), topology="a")
        fast = variant_of(medium, "same_group_fast")
        other_topology = example(1, (15, None, None, None), topology="b")
        same_topology = example(2, (20, None, None, None), topology="a")
        rows = [medium, fast, other_topology, same_topology]
        preferred = plan(rows, self.counts)
        self.assertEqual(preferred["report"]["reserve_reassignments"][0]["replacement_task_id"], "screen_2")
        self.assertEqual(preferred["report"]["selected_topologies"], {"a": 2})
        capped = plan(rows, self.counts, per_topology_limit=1)
        self.assertTrue(capped["report"]["complete"])
        self.assertEqual(capped["report"]["reserve_reassignments"][0]["replacement_task_id"], "screen_1")
        self.assertFalse(capped["report"]["reserve_reassignments"][0]["same_topology_replacement"])
        self.assertEqual(capped["report"]["selected_topologies"], {"a": 1, "b": 1})
        blocked = plan([medium, fast, same_topology], self.counts, per_topology_limit=1)
        self.assertFalse(blocked["report"]["complete"])
        self.assertEqual(blocked["report"]["reserve_reassignments"], [])
        self.assertEqual(blocked["report"]["selected_topologies"], {"a": 1})

    def test_replacement_may_not_worsen_displaced_priority_tier(self):
        medium = example(0, (12, None, None, None))
        fast = variant_of(medium, "same_group_fast")
        set_reference(fast, 16.0)
        reserve = example(1, (4, None, None, None))
        # Initially this fallback conflicts with the selected medium reference.
        # Moving the first group to its alternate reference would make it fit,
        # but filling the displaced medium slot with tier1 is prohibited.
        set_reference(reserve, 4.001)
        blocked = plan([medium, fast, reserve], self.counts)
        self.assertFalse(blocked["report"]["complete"])
        self.assertEqual(blocked["report"]["reserve_reassignments"], [])
        self.assertEqual(blocked["ids"], ["screen_0"])
        reserve["observation"]["methods"] = example(1, (20, None, None, None))["observation"]["methods"]
        repaired = plan([medium, fast, reserve], self.counts)
        self.assertTrue(repaired["report"]["complete"])
        self.assertEqual(repaired["report"]["fallback_selected"]["medium_priority"], 0)
        self.assertEqual(repaired["report"]["reserve_reassignments"][0]["replacement_priority_tier"], 0)

    def test_combined_change_checks_new_reference_collision_and_distance(self):
        for reference in (16.0, 16.001):
            with self.subTest(reference=reference):
                medium = example(0, (12, None, None, None))
                fast = variant_of(medium, "same_group_fast")
                set_reference(fast, 16.0)
                reserve = example(1, (20, None, None, None))
                set_reference(reserve, reference)
                result = plan([medium, fast, reserve], self.counts)
                self.assertFalse(result["report"]["complete"])
                self.assertEqual(result["ids"], ["screen_0"])
                self.assertEqual(result["report"]["reserve_reassignments"], [])
                self.assertEqual(result["report"]["shortfalls"]["fast"], 1)

    def test_unrepairable_group_shortfall_remains_explicitly_partial(self):
        medium = example(0, (12, None, None, None))
        fast = variant_of(medium, "same_group_fast")
        result = plan([medium, fast], self.counts)
        self.assertFalse(result["report"]["complete"])
        self.assertEqual(result["report"]["shortfalls"], {"fast": 1, "medium_priority": 0, "hard_priority": 0})
        self.assertEqual(result["report"]["reserve_reassignments"], [])
        self.assertEqual(result["report"]["reserve_reassignment_policy"]["maximum_steps"], 1)

    def test_hard_reserve_can_release_a_fast_group_without_weakening_hard_priority(self):
        hard = example(0, (None,) * 4, variant="distant")
        fast = variant_of(hard, "same_group_fast")
        reserve = example(1, (None,) * 4, variant="distant")
        result = plan([hard, fast, reserve], {"fast": 1, "medium_priority": 0, "hard_priority": 1})
        self.assertTrue(result["report"]["complete"])
        repair, = result["report"]["reserve_reassignments"]
        self.assertEqual(repair["displaced_queue"], "hard_priority")
        self.assertEqual(repair["replacement_priority_tier"], 0)
        self.assertEqual(result["report"]["fallback_selected"]["hard_priority"], 0)

    def test_multiple_repairs_each_fill_one_slot_and_preserve_other_quotas(self):
        first = example(0, (12, None, None, None))
        second = example(1, (13, None, None, None))
        rows = [first, second, variant_of(first, "first_fast"), variant_of(second, "second_fast"),
                example(2, (25, None, None, None)), example(3, (28, None, None, None))]
        counts = {"fast": 2, "medium_priority": 2, "hard_priority": 0}
        result = plan(rows, counts)
        self.assertTrue(result["report"]["complete"])
        self.assertEqual(result["report"]["greedy_selected_queues"]["fast"], 0)
        self.assertEqual(result["report"]["selected_queues"], counts)
        self.assertEqual(len(result["report"]["reserve_reassignments"]), 2)
        self.assertEqual(result["report"]["reserve_reassignment_policy"]["maximum_steps"], 2)
        self.assertEqual(len(set(result["ids"])), 4)
        self.assertEqual(result["report"]["selected_unique_requirement_groups"], 4)


class ConfirmationFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="confirmation_plan_test_", dir=ROOT / "runs")
        self.root = Path(self.temporary.name)
        self.candidates, self.calibration = self.root / "candidates", self.root / "screen"
        domain = deepcopy(json.loads((ROOT / "benchmark/domains.json").read_text())["entries"][0])
        task = deepcopy(domain["task"])
        task["id"] = "planning_fixture"
        task["initial_parameters"] = rounded_parameters(task, task["initial_parameters"])
        reference = deepcopy(task["initial_parameters"])
        name = next(iter(reference))
        rule = task["parameters"][name]
        reference[name] = reference[name] * 2 if reference[name] * 2 <= rule["max"] else reference[name] / 2
        reference = rounded_parameters(task, reference)
        candidate = {"id": task["id"], "topology": domain["name"], "topology_family": domain["family"],
                     "proposed_difficulty": "easy", "profile": "balanced", "task": task, "reference": reference,
                     "start_variant": "nearest", "requirement_group": canonical_hash(requirement_identity(task))}
        self.candidate_path = self.candidates / "candidates" / (task["id"] + ".json")
        save_json(self.candidate_path, candidate)
        save_json(self.candidates / "index.json", {"tasks": [{key: candidate[key] for key in
                  ("id", "topology", "topology_family", "proposed_difficulty", "profile")} |
                  {"path": "candidates/" + task["id"] + ".json"}]})
        physical = {key: task[key] for key in PHYSICAL_FIELDS}
        physical_key = canonical_hash(physical)
        inputs = [self.candidates / "index.json", self.candidate_path, ROOT / "benchmark/domains.json",
                  *[ROOT / path for path in CODE_FILES]]
        config = {"methods": list(METHODS), "seeds": [0], "candidate_ids": [task["id"]], "budget": 30,
                  "simulator_timeout_s": 30, "inputs_sha256": {str(path.resolve()): digest(path) for path in inputs},
                  "physics": {physical_key: canonical_hash(physical)}}
        self.manifest_path = self.calibration / "manifest.json"
        save_json(self.manifest_path, {"schema_version": 1, "configuration": config,
                  "configuration_sha256": canonical_hash(config), "physical_identities": {physical_key: physical}})
        identity = {"configuration_sha256": canonical_hash(config), "candidate_sha256": digest(self.candidate_path),
                    "task_sha256": canonical_hash(task), "reference_sha256": canonical_hash(reference),
                    "public_default_sha256": canonical_hash(domain["public_default"]), "physics_sha256": canonical_hash(physical)}

        def measured(passes):
            metrics = {key: (limits["min"] * 1.2 if "min" in limits else limits["max"] * 0.5)
                       for key, limits in task["constraints"].items()}
            if not passes:
                metrics["gain_db"] = task["constraints"]["gain_db"]["min"] - 5
            return {"status": "ok", "metrics": metrics, "independent_metrics": deepcopy(metrics),
                    **score(metrics, task["constraints"])}

        initial, passed = measured(False), measured(True)
        admission = {"id": task["id"], "topology": domain["name"], "effective_task": task,
                     "rounded_reference_parameters": reference, "public_default_parameters": domain["public_default"],
                     "logical_evaluations": 3, **admission_decision(task, initial, passed, initial),
                     "evaluations": {"initial": initial, "reference": passed, "public_seed": initial}}
        self.admission_path = self.calibration / task["id"] / "admission.json"
        save_checkpoint(self.admission_path, {**identity, "kind": "admission"}, admission)
        trials = {}
        for method in METHODS:
            evaluations = []

            def evaluate(parameters):
                evaluations.append(parameters)
                return measured(len(evaluations) == 2)

            trial = run_search(task, evaluate, method, 0)
            for step in trial["trajectory"]:
                step["independent_metrics"] = deepcopy(step["metrics"])
            save_checkpoint(self.calibration / task["id"] / f"{method}_0.json",
                            {**identity, "kind": "baseline", "method": method, "seed": 0}, trial)
            trials[(task["id"], method, 0)] = trial
        summary = make_summary([{"candidate": candidate}], {task["id"]: admission}, trials,
                               methods=list(METHODS), seeds=[0], accounting={}, complete=True)
        self.summary_path = self.calibration / "summary.json"
        save_json(self.summary_path, summary)
        self.output = self.root / "plan.json"

    def tearDown(self):
        self.temporary.cleanup()

    def run_plan(self):
        return plan_files(self.candidates, [self.calibration], self.output,
                          {"fast": 1, "medium_priority": 0, "hard_priority": 0})

    def test_real_checkpoint_loader_and_baseline_replay_produce_only_provisional_ids(self):
        result = self.run_plan()
        self.assertEqual(result["ids"], ["planning_fixture"])
        self.assertEqual(result["difficulty_status"], "unassigned")
        self.assertIn(str(self.admission_path), result["inputs_sha256"])
        self.assertEqual(json.loads(self.output.read_text()), result)
        with self.assertRaises(FileExistsError):
            self.run_plan()

    def test_incomplete_summary_and_forged_trial_counts_are_rejected(self):
        summary = json.loads(self.summary_path.read_text())
        for changed in ({**summary, "status": "partial"},
                        {**summary, "completed_trials": 40}):
            save_json(self.summary_path, changed)
            with self.assertRaises(ValueError):
                self.run_plan()
            self.assertFalse(self.output.exists())

    def test_missing_or_modified_trial_checkpoint_does_not_become_a_completed_seed(self):
        path = self.calibration / "planning_fixture" / (METHODS[0] + "_0.json")
        record = json.loads(path.read_text())
        record["evaluations_used"] = 30
        save_json(path, record)
        with self.assertRaisesRegex(ValueError, "content hash"):
            self.run_plan()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing a method trial"):
            self.run_plan()
        self.assertFalse(self.output.exists())

    def test_changed_candidate_or_admission_identity_is_rejected(self):
        record = json.loads(self.admission_path.read_text())
        identity = record["_checkpoint"]["identity"]
        payload = {key: value for key, value in record.items() if key != "_checkpoint"}
        payload["effective_task"]["purpose"] = "Changed task"
        save_checkpoint(self.admission_path, identity, payload)
        with self.assertRaisesRegex(ValueError, "admission task"):
            self.run_plan()
        self.assertFalse(self.output.exists())

    def test_extra_screening_seeds_and_missing_fourth_method_are_rejected(self):
        original = json.loads(self.manifest_path.read_text())
        for field, value in (("seeds", [0, 1]), ("methods", list(METHODS[:-1]))):
            manifest = deepcopy(original)
            manifest["configuration"][field] = value
            manifest["configuration_sha256"] = canonical_hash(manifest["configuration"])
            save_json(self.manifest_path, manifest)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "one-seed, four-method"):
                self.run_plan()
            self.assertFalse(self.output.exists())

    def test_changed_indexed_candidate_fails_its_frozen_input_binding(self):
        self.candidate_path.write_text(self.candidate_path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "frozen input"):
            self.run_plan()
        self.assertFalse(self.output.exists())

    def test_input_mutation_during_planning_prevents_output(self):
        original = plan

        def mutate(observations, counts, **options):
            result = original(observations, counts, **options)
            self.candidate_path.write_text(self.candidate_path.read_text() + "\n")
            return result

        with patch("benchmark.confirmation_plan.plan", side_effect=mutate), self.assertRaisesRegex(RuntimeError, "input changed"):
            self.run_plan()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
