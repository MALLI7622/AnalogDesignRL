"""Numerical and curriculum contracts for joint target curation."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.metrics import score
from benchmark import frontier_candidates as frontier
from benchmark.candidates import reference_margin


def measured(**changes):
    return {"gain_db": 100.8, "unity_gain_hz": 2.21e6, "phase_margin_deg": 80.8,
            "power_w": 1e-4, "dc_error_v": 0.001, "max_tracking_error_v": 0.001,
            "settling_rise_s": 1e-6, "settling_fall_s": 1e-6, **changes}


def entry():
    public = {"W": 1.0, "IBIAS": 2.9999999999999997e-5}
    task = {"id": "fixture", "circuit_directory": "circuits/fixture", "subcircuit": "fixture",
            "conditions": {"supply_v": 1.8}, "ac": {}, "transient": {},
            "max_evaluations": 30, "initial_parameters": deepcopy(public),
            "parameters": {"W": {"min": 1, "max": 100, "unit": "um"},
                           "IBIAS": {"min": 1e-5, "max": 1e-3, "unit": "A"}},
            "constraints": {"gain_db": {"min": 50}, "unity_gain_hz": {"min": 1e5},
                            "phase_margin_deg": {"min": 60}, "power_w": {"max": 0.002},
                            "dc_error_v": {"max": 0.02}, "max_tracking_error_v": {"max": 0.02},
                            "settling_rise_s": {"max": 1e-5}, "settling_fall_s": {"max": 1e-5}}}
    return {"name": "fixture", "family": "fixture_family", "public_default": public, "task": task}


def row(name, width, metrics, *, bias=3e-5):
    return {"status": "ok", "parameters": {"W": width, "IBIAS": bias},
            "metrics": metrics, "measurement_directory": "runs/fixture/" + name}


class FrontierTargetsTests(unittest.TestCase):
    def setUp(self):
        self.domain = entry()
        self.public = row("public", 1, measured(gain_db=45))
        self.fast = row("fast", 4, measured())
        self.cheap = row("cheap", 16, measured(unity_gain_hz=1.23e6, power_w=5e-5))

    def test_target_relative_margin_math_rounds_outward(self):
        constraints = frontier.tight_constraints(self.domain["task"], measured())
        # 2.2 MHz would have only 0.45% slack and must not be accepted.
        self.assertEqual(constraints["unity_gain_hz"], {"min": 2e6})
        self.assertEqual(constraints["power_w"], {"max": 1.2e-4})
        self.assertEqual(constraints["gain_db"], {"min": 100})
        self.assertEqual(constraints["phase_margin_deg"], {"min": 80})
        self.assertTrue(reference_margin(measured(), constraints))

    def test_absolute_settling_margin_is_retained_at_short_times(self):
        metrics = measured(settling_rise_s=1e-8)
        constraints = frontier.tight_constraints(self.domain["task"], metrics)
        self.assertGreaterEqual(constraints["settling_rise_s"]["max"] - 1e-8, 12e-9)
        self.assertTrue(reference_margin(metrics, constraints))

    def test_domain_floor_cannot_be_weakened_to_accept_marginal_reference(self):
        self.assertIsNone(frontier.tight_constraints(self.domain["task"], measured(phase_margin_deg=60.1)))
        self.assertIsNone(frontier.tight_constraints(self.domain["task"], measured(power_w=0.002)))

    def test_nonfinite_data_and_unusable_grid_values_are_excluded(self):
        for value in (math.nan, math.inf, -math.inf):
            self.assertIsNone(frontier.tight_constraints(self.domain["task"], measured(gain_db=value)))
        self.assertIsNone(frontier.tight_constraints(self.domain["task"], measured(unity_gain_hz=0)))

    def test_public_default_rounding_and_missing_evidence(self):
        targets, report = frontier.choose_targets(self.domain, [self.public, self.fast], count=1)
        self.assertEqual(len(targets), 1)
        self.assertTrue(report["complete"])
        with self.assertRaisesRegex(ValueError, "Missing measured public default"):
            frontier.choose_targets(self.domain, [self.fast])

    def test_public_seed_win_excludes_other_reference_target(self):
        public = row("public", 1, measured(gain_db=120, unity_gain_hz=8e6,
                     phase_margin_deg=95, power_w=1e-5, dc_error_v=1e-5,
                     max_tracking_error_v=1e-5, settling_rise_s=1e-8, settling_fall_s=1e-8))
        targets, report = frontier.choose_targets(self.domain, [public, self.fast], count=1)
        self.assertEqual(targets, [])
        self.assertFalse(report["complete"])
        self.assertGreater(report["rejections"]["public_default_passes"], 0)

    def test_target_set_diversity_and_no_count_padding(self):
        targets, report = frontier.choose_targets(self.domain, [self.public, self.fast, self.cheap], count=5)
        self.assertEqual(len(targets), 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["coverage"]["max_single_design_coverage"], 1)
        self.assertEqual(report["coverage"]["greedy_reference_cover"], 2)
        self.assertEqual(report["coverage"]["uncovered_task_count"], 0)
        self.assertFalse(report["training_approved"])
        self.assertEqual(report["calibration_status"], "not_calibrated")
        self.assertNotEqual(targets[0]["empirical_solution_signature"], targets[1]["empirical_solution_signature"])

    def test_duplicate_roundings_and_feasible_sets_do_not_inflate_count(self):
        clone = row("clone", 4.00001, measured())
        distant_same_results = row("distant", 30, measured())
        targets, report = frontier.choose_targets(self.domain, [self.public, self.fast, clone, distant_same_results], count=5)
        self.assertEqual(report["valid_distinct_rounded_designs"], 3)
        self.assertEqual(len(targets), 1)

    def test_reference_parameter_separation_is_enforced(self):
        close = row("close", 4.01, measured(unity_gain_hz=1.23e6, power_w=5e-5))
        targets, report = frontier.choose_targets(self.domain, [self.public, self.fast, close], count=2)
        self.assertEqual(len(targets), 1)
        self.assertFalse(report["complete"])

    def test_selection_is_independent_of_input_row_order(self):
        rows = [self.public, self.fast, self.cheap]
        first, report = frontier.choose_targets(self.domain, rows, count=2)
        second, second_report = frontier.choose_targets(self.domain, list(reversed(rows)), count=2)
        self.assertEqual(first, second)
        self.assertEqual(report, second_report)

    def test_integer_and_out_of_bounds_rows_are_excluded(self):
        domain = deepcopy(self.domain)
        domain["task"]["parameters"]["W"]["integer"] = True
        fractional = row("fractional", 4.5, measured())
        outside = row("outside", 101, measured())
        targets, report = frontier.choose_targets(domain, [self.public, self.fast, fractional, outside], count=2)
        self.assertEqual(report["valid_distinct_rounded_designs"], 2)
        self.assertEqual(len(targets), 1)

    def test_invalid_count_and_parameter_bounds_fail_explicitly(self):
        for count in (0, -1, 1.5, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                frontier.choose_targets(self.domain, [self.public, self.fast], count=count)
        for low, high in ((0, 100), (5, 5), (10, 1)):
            domain = deepcopy(self.domain)
            domain["task"]["parameters"]["W"].update(min=low, max=high)
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                frontier.choose_targets(domain, [self.public, self.fast])

    def test_invalid_public_default_is_not_silently_clamped_to_a_measured_bound(self):
        domain = deepcopy(self.domain)
        domain["public_default"]["W"] = 0.5
        with self.assertRaisesRegex(ValueError, "Public default is outside"):
            frontier.choose_targets(domain, [self.public, self.fast])


class FrontierStartTests(unittest.TestCase):
    def setUp(self):
        self.domain = entry()
        self.public = row("public", 1, measured(gain_db=45))
        self.reference = row("reference", 4, measured())
        self.targets, _ = frontier.choose_targets(self.domain, [self.public, self.reference], count=1)
        self.target = self.targets[0]

    def test_near_boundary_failure_is_excluded(self):
        limits = self.target["constraints"]
        weak = measured(gain_db=limits["gain_db"]["min"] - 0.01)
        self.assertFalse(score(weak, limits)["success"])
        self.assertEqual(frontier.clear_failures(weak, limits), [])
        start = row("weak", 2, weak)
        candidates, report = frontier.build_candidates(self.domain, [start], self.targets)
        self.assertEqual(candidates, [])
        self.assertEqual(report["rejections"]["no_clear_valid_failing_start"], 1)

    def test_passing_invalid_and_same_vector_starts_are_excluded(self):
        invalid = row("invalid", 2, measured(gain_db=45))
        invalid["status"] = "failed"
        same = row("same", 4, measured(gain_db=45))
        passing = row("passing", 2, measured())
        candidates, _ = frontier.build_candidates(self.domain, [invalid, same, passing], self.targets)
        self.assertEqual(candidates, [])

    def test_target_tagged_start_yields_compatible_unapproved_candidate_schema(self):
        start = {**row("changed", 2, measured(gain_db=95)), "target_key": self.target["key"],
                 "perturbation": {"W": "half"}}
        wrong = {**row("wrong", 3, measured(gain_db=90)), "target_key": "another_target"}
        generated, report = frontier.build_candidates(self.domain, [], self.targets, starts=[wrong, start])
        self.assertTrue(report["complete"])
        self.assertEqual(len(generated), 1)
        candidate = generated[0]
        self.assertEqual(candidate["task"]["initial_parameters"]["W"], 2)
        self.assertEqual(candidate["reference"], self.target["parameters"])
        self.assertEqual(candidate["requirement_group"], self.target["requirement_group"])
        self.assertEqual(candidate["characterization_initial_clear_failures"], ["gain_db"])
        self.assertEqual(candidate["calibration_status"], "pending")
        self.assertFalse(candidate["training_approved"])
        self.assertEqual(candidate["profile"], "balanced")
        self.assertIn(candidate["proposed_difficulty"], {"easy", "medium", "hard"})

    def test_nonfinite_start_has_no_clear_failure_evidence(self):
        self.assertEqual(frontier.clear_failures(measured(gain_db=math.nan), self.target["constraints"]), [])

    def test_loading_preserves_distinct_target_tags_on_one_validated_start_vector(self):
        first = {**row("changed", 2, measured(gain_db=95)), "target_key": "one"}
        second = {**deepcopy(first), "target_key": "two"}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "fixture.json").write_text(json.dumps([first, second]))
            with patch.object(frontier, "load_designs", return_value=[second]) as validate:
                starts = frontier.load_starts([directory], self.domain)
            validate.assert_called_once_with([directory], self.domain)
            self.assertEqual([start["target_key"] for start in starts], ["one", "two"])

    def test_controlled_starts_retain_intermediate_and_distant_global_variants(self):
        controlled = {**row("controlled", 3, measured(gain_db=95)), "target_key": self.target["key"],
                      "perturbation_kind": "single_bias", "requested_factors": {"IBIAS": 2},
                      "actual_factors": {"IBIAS": 2}, "changed_controls": ["IBIAS"],
                      "normalized_reference_distance": 0.01}
        intermediate = row("intermediate", 15, measured(gain_db=90, phase_margin_deg=70), bias=6e-5)
        distant = row("distant", 95, measured(gain_db=80, phase_margin_deg=65))
        generated, report = frontier.build_candidates(self.domain, [intermediate, distant], self.targets,
                                                      starts=[controlled])
        self.assertEqual([candidate["start_variant"] for candidate in generated], list(frontier.START_VARIANTS))
        self.assertEqual([candidate["start_pool"] for candidate in generated], ["controlled", "global", "global"])
        self.assertEqual([candidate["task"]["initial_parameters"]["W"] for candidate in generated], [3, 15, 95])
        self.assertEqual(len({candidate["id"] for candidate in generated}), 3)
        self.assertEqual(len({candidate["requirement_group"] for candidate in generated}), 1)
        self.assertTrue(all(candidate["reference"] == self.target["parameters"] for candidate in generated))
        self.assertEqual(generated[0]["start_perturbation"]["perturbation_kind"], "single_bias")
        self.assertEqual(generated[0]["start_perturbation"]["requested_factors"], {"IBIAS": 2})
        self.assertTrue(report["all_variants_available"])
        self.assertEqual(report["targets_with_candidates"], 1)

    def test_variants_never_repeat_a_rounded_start_to_fill_count(self):
        first = row("first", 3, measured(gain_db=95, phase_margin_deg=70))
        duplicate = row("duplicate", 3.00001, measured(gain_db=95, phase_margin_deg=70))
        generated, report = frontier.build_candidates(self.domain, [first, duplicate], self.targets)
        self.assertEqual(len(generated), 1)
        self.assertFalse(report["all_variants_available"])
        self.assertEqual(report["targets_with_candidates"], 1)

    def test_intermediate_requires_two_clear_failures_and_can_be_selected_alone(self):
        one = row("one", 15, measured(gain_db=95))
        generated, report = frontier.build_candidates(self.domain, [one], self.targets, variants=["intermediate"])
        self.assertEqual(generated, [])
        self.assertFalse(report["complete"])
        self.assertEqual(report["rejections"]["missing_distinct_intermediate_start"], 1)
        for variants in ([], ["nearest", "nearest"], ["unsupported"]):
            with self.subTest(variants=variants), self.assertRaises(ValueError):
                frontier.build_candidates(self.domain, [one], self.targets, variants=variants)

    def test_candidate_variant_selection_does_not_depend_on_row_order(self):
        rows = [row("one", 3, measured(gain_db=95)),
                row("two", 15, measured(gain_db=95, phase_margin_deg=70)),
                row("three", 95, measured(gain_db=90, phase_margin_deg=65))]
        self.assertEqual(frontier.build_candidates(self.domain, rows, self.targets),
                         frontier.build_candidates(self.domain, list(reversed(rows)), self.targets))

    def test_intermediate_prefers_actual_two_control_start_over_nominal_tags(self):
        clipped = {**row("clipped", 3, measured(gain_db=95, phase_margin_deg=70)),
                   "target_key": self.target["key"], "perturbation_kind": "capacitance_and_bias",
                   "changed_controls": ["W", "IBIAS"]}
        paired = {**row("paired", 2, measured(gain_db=95), bias=6e-5),
                  "target_key": self.target["key"], "perturbation_kind": "incorrect_single_tag"}
        global_start = row("global", 15, measured(gain_db=95, phase_margin_deg=70), bias=6e-5)
        candidates, report = frontier.build_candidates(self.domain, [global_start], self.targets,
            starts=[clipped, paired], variants=["intermediate"])
        self.assertTrue(report["complete"])
        self.assertEqual(candidates[0]["task"]["initial_parameters"], paired["parameters"])
        self.assertEqual(candidates[0]["start_pool"], "controlled")
        self.assertEqual(candidates[0]["substantive_changed_controls"], ["IBIAS", "W"])
        fallback, _ = frontier.build_candidates(self.domain, [global_start], self.targets,
                                                starts=[clipped], variants=["intermediate"])
        self.assertEqual(fallback[0]["start_pool"], "global")
        self.assertEqual(fallback[0]["task"]["initial_parameters"], global_start["parameters"])

    def test_substantive_change_count_uses_real_domain_scaled_values(self):
        parameters = self.target["parameters"]
        tiny = {**parameters, "W": parameters["W"] * 1.001}
        self.assertEqual(frontier.substantive_changes(self.domain["task"], parameters, tiny), [])
        actual = {"W": parameters["W"] * 2, "IBIAS": parameters["IBIAS"] * 1.2}
        self.assertEqual(frontier.substantive_changes(self.domain["task"], parameters, actual), ["IBIAS", "W"])
        mixed = {"parameters": {"M": {"min": 1, "max": 1000, "integer": True},
                                "V": {"min": -2, "max": 2}, "F": {"min": 1, "max": 1}}}
        self.assertEqual(frontier.substantive_changes(mixed, {"M": 900, "V": 0, "F": 1},
                                                      {"M": 901, "V": 0.11, "F": 1}), ["M", "V"])

    def test_loading_detects_mutation_between_validation_and_tag_reread(self):
        first = {**row("changed", 2, measured(gain_db=95)), "target_key": "one"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            path.write_text(json.dumps([first]))

            def mutate(*args):
                second = {**deepcopy(first), "target_key": "different"}
                path.write_text(json.dumps([second]))
                return [first]

            with patch.object(frontier, "load_designs", side_effect=mutate), self.assertRaisesRegex(RuntimeError, "input changed"):
                frontier.load_starts([Path(temporary)], self.domain)


class SavedFrontierTargetsTests(unittest.TestCase):
    def setUp(self):
        self.domain = entry()
        rows = [row("public", 1, measured(gain_db=45)), row("fast", 4, measured()),
                row("cheap", 16, measured(unity_gain_hz=1.23e6, power_w=5e-5))]
        self.targets, _ = frontier.choose_targets(self.domain, rows, count=2)

    def load(self, targets, maximum=None):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "targets.json"
            path.write_text(json.dumps({"targets": targets}))
            with patch.object(frontier, "_validate_target_archive") as archive:
                result = frontier.load_fixed_targets(path, [self.domain], maximum_per_topology=maximum)
            return result, archive.call_count

    def test_saved_definitions_and_order_preserved_without_target_regeneration(self):
        with patch.object(frontier, "choose_targets", side_effect=AssertionError("must not choose again")):
            loaded, count = self.load(self.targets, maximum=1)
        self.assertEqual(loaded, self.targets[:1])
        self.assertEqual(count, 2)  # Validate the whole source file before limiting.

    def test_changed_reference_rounding_constraints_and_requirement_identity_fail(self):
        for field, value in (("parameters", {"W": 5, "IBIAS": 3e-5}),
                             ("characterization_key", "wrong"),
                             ("reference_measured_after_rounding", False),
                             ("requirement_group", "wrong"), ("constraints", {})):
            target = deepcopy(self.targets[0])
            target[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load([target])

    def test_repeated_targets_and_support_metadata_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "repeat"):
            self.load([self.targets[0], self.targets[0]])
        for field, value in (("empirical_solution_signature", "wrong"),
                             ("empirical_pool_valid_count", 0),
                             ("empirical_pool_feasible_count", True),
                             ("empirical_feasible_design_keys", [])):
            target = deepcopy(self.targets[0])
            target[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "support metadata"):
                self.load([target])

    def test_target_archive_binds_source_settings_and_formerly_fixed_controls(self):
        target = deepcopy(self.targets[0])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            circuit = root / self.domain["task"]["circuit_directory"]
            archive = root / target["measurement_directory"]
            circuit.mkdir(parents=True)
            archive.mkdir(parents=True)
            (root / "analog_design").mkdir()
            for name in ("netlist.spice", "reference.params"):
                (circuit / name).write_text("fixed source bytes")
            for name in ("simulator.py", "metrics.py", "crosscheck.py"):
                (root / "analog_design" / name).write_text("fixed evaluator bytes")
            parameters = target["measured_parameters"]
            (archive / "parameters.spice").write_text(f".param W={parameters['W']} IBIAS={parameters['IBIAS']}")
            source_paths = {"netlist_sha256": circuit / "netlist.spice",
                            "reference_parameters_sha256": circuit / "reference.params",
                            "parameters_sha256": archive / "parameters.spice",
                            "evaluator_sha256": root / "analog_design/simulator.py",
                            "metrics_code_sha256": root / "analog_design/metrics.py",
                            "crosscheck_code_sha256": root / "analog_design/crosscheck.py"}
            evidence = {"status": "ok", "metrics": target["metrics"],
                        "parameters": {"W": parameters["W"]},
                        "provenance": {key: frontier.digest(path) for key, path in source_paths.items()}}
            old_task = deepcopy(self.domain["task"])
            old_task["circuit_directory"] = "older/copied/circuit"
            del old_task["parameters"]["IBIAS"]
            (archive / "result.json").write_text(json.dumps(evidence))
            (archive / "task.json").write_text(json.dumps(old_task))
            with patch.object(frontier, "ROOT", root):
                frontier._validate_target_archive(self.domain, target)
                wrong = deepcopy(target)
                wrong["measured_parameters"]["IBIAS"] *= 2
                with self.assertRaisesRegex(ValueError, "disagree with its archive"):
                    frontier._validate_target_archive(self.domain, wrong)
                old_task["conditions"]["supply_v"] = 3.3
                (archive / "task.json").write_text(json.dumps(old_task))
                with self.assertRaisesRegex(ValueError, "disagree with its archive"):
                    frontier._validate_target_archive(self.domain, target)
                (archive / "task.json").write_text(json.dumps(self.domain["task"]))
                (circuit / "netlist.spice").write_text("changed source bytes")
                with self.assertRaisesRegex(ValueError, "disagree with its archive"):
                    frontier._validate_target_archive(self.domain, target)


if __name__ == "__main__":
    unittest.main()
