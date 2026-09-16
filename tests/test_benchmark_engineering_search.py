"""Budgeted physical-factor search contracts; no circuit simulation is needed."""
from copy import deepcopy
import math
import unittest

from benchmark.baselines import run_search
from benchmark.engineering_search import METHOD, run_engineering_sweep


def task():
    return {"id": "engineering_fixture", "max_evaluations": 30,
            "parameters": {"C": {"min": 1e-12, "max": 64e-12, "unit": "F"},
                           "I": {"min": 0.25e-6, "max": 64e-6, "unit": "A"},
                           "M": {"min": 1, "max": 64, "unit": "multiplicity", "integer": True}},
            "initial_parameters": {"C": 8e-12, "I": 4e-6, "M": 8}}


def failed(action):
    return {"status": "ok", "success": False, "reward": -0.5,
            "metrics": {"distance": 0.5}, "checks": {"distance": False}}


def objective(target):
    def evaluate(action):
        loss = sum(abs(math.log(action[name] / value)) for name, value in target.items())
        success = loss < 1e-12
        return {"status": "ok", "success": success, "reward": 1 if success else -loss / (1 + loss),
                "metrics": {"distance": loss}, "checks": {"distance": success}}
    return evaluate


class EngineeringSweepTests(unittest.TestCase):
    def test_full_anchored_half_double_sweep_precedes_feedback(self):
        definition = task()
        result = run_engineering_sweep(definition, failed, 7, budget=7)
        initial = definition["initial_parameters"]
        self.assertEqual(result["trajectory"][0]["parameters"], initial)
        probes = set()
        for step in result["trajectory"][1:]:
            changed = [name for name in initial if step["parameters"][name] != initial[name]]
            self.assertEqual(len(changed), 1)
            name = changed[0]
            probes.add((name, step["parameters"][name] / initial[name]))
        self.assertEqual(probes, {(name, factor) for name in initial for factor in (0.5, 2.0)})
        self.assertEqual(result["evaluations_used"], 7)

    def test_single_control_repairs_are_found_within_two_n_plus_one(self):
        definition = task()
        for name in definition["parameters"]:
            for factor in (0.5, 2.0):
                target = {**definition["initial_parameters"], name: definition["initial_parameters"][name] * factor}
                for seed in range(8):
                    with self.subTest(name=name, factor=factor, seed=seed):
                        result = run_engineering_sweep(definition, objective(target), seed)
                        self.assertTrue(result["success"])
                        self.assertLessEqual(result["evaluations_used"], 2 * len(target) + 1)
                        self.assertEqual(result["best_parameters"], target)

    def test_two_control_improving_moves_are_combined_from_observed_feedback(self):
        definition = task()
        target = {**definition["initial_parameters"], "C": 16e-12, "I": 2e-6}
        for seed in range(8):
            result = run_engineering_sweep(definition, objective(target), seed)
            self.assertTrue(result["success"])
            self.assertEqual(result["evaluations_used"], 8)
            self.assertEqual(result["trajectory"][-1]["parameters"], target)
            self.assertTrue(all(not step["success"] for step in result["trajectory"][:-1]))

    def test_post_sweep_refinement_uses_one_and_half_factors(self):
        definition = {"id": "fine", "max_evaluations": 30,
                      "parameters": {"R": {"min": 1.0, "max": 16.0, "unit": "ohm"}},
                      "initial_parameters": {"R": 4.0}}
        result = run_engineering_sweep(definition, failed, 1, budget=5)
        self.assertEqual({step["parameters"]["R"] for step in result["trajectory"][:3]}, {2.0, 4.0, 8.0})
        fine = sorted(step["parameters"]["R"] for step in result["trajectory"][3:])
        self.assertAlmostEqual(fine[0], 4 * 2 / 3)
        self.assertEqual(fine[1], 6.0)

    def test_full_schema_determinism_unique_actions_and_task_immutability(self):
        definition = task()
        first = run_engineering_sweep(definition, failed, 17)
        self.assertEqual(first, run_engineering_sweep(definition, failed, 17))
        self.assertNotEqual(first["trajectory"], run_engineering_sweep(definition, failed, 18)["trajectory"])
        comparison = run_search(definition, failed, "coordinate_search", 17, budget=1)
        self.assertEqual(set(first), set(comparison))
        self.assertEqual(set(first["trajectory"][0]), set(comparison["trajectory"][0]))
        self.assertEqual(first["method"], METHOD)
        self.assertEqual(first["evaluations_used"], 30)
        self.assertEqual(first["stop_reason"], "budget_exhausted")
        vectors = [tuple(sorted(step["parameters"].items())) for step in first["trajectory"]]
        self.assertEqual(len(set(vectors)), len(vectors))
        self.assertEqual([step["evaluation"] for step in first["trajectory"]], list(range(1, 31)))
        self.assertEqual(definition, task())

    def test_failed_calls_are_charged_and_cannot_claim_success(self):
        calls = []

        def evaluate(action):
            calls.append(action)
            return {"status": "timeout", "success": True, "reward": 1.0}

        result = run_engineering_sweep(task(), evaluate, 0, budget=12)
        self.assertEqual(len(calls), 12)
        self.assertEqual(result["evaluations_used"], 12)
        self.assertFalse(result["success"])
        self.assertTrue(all(step["reward"] == -1 for step in result["trajectory"]))

    def test_bounds_rounding_and_integer_exhaustion_skip_duplicate_calls(self):
        definition = {"id": "discrete", "max_evaluations": 30, "initial_parameters": {"M": 1},
                      "parameters": {"M": {"min": 1, "max": 3, "integer": True}}}
        result = run_engineering_sweep(definition, failed, 11)
        self.assertEqual(result["evaluations_used"], 3)
        self.assertEqual(result["stop_reason"], "no_unique_candidate")
        self.assertEqual({step["parameters"]["M"] for step in result["trajectory"]}, {1, 2, 3})
        self.assertTrue(all(type(step["parameters"]["M"]) is int for step in result["trajectory"]))
        definition["parameters"]["M"]["max"] = 1
        result = run_engineering_sweep(definition, failed, 11)
        self.assertEqual(result["evaluations_used"], 1)
        self.assertEqual(result["stop_reason"], "no_unique_candidate")

    def test_signed_and_zero_parameters_use_bounded_linear_fallback(self):
        definition = {"initial_parameters": {"V": 0.0}, "max_evaluations": 30,
                      "parameters": {"V": {"min": -1.0, "max": 1.0, "unit": "V"}}}
        result = run_engineering_sweep(definition, failed, 0, budget=10)
        self.assertEqual({step["parameters"]["V"] for step in result["trajectory"][:3]}, {-0.5, 0.0, 0.5})
        self.assertEqual(result["evaluations_used"], 10)
        self.assertTrue(all(-1 <= step["parameters"]["V"] <= 1 for step in result["trajectory"]))

    def test_initial_is_charged_and_success_stops_immediately(self):
        definition = task()
        definition["max_evaluations"] = 2
        calls = []

        def evaluate(action):
            calls.append(action)
            return {**failed(action), "success": len(calls) == 2, "reward": 1 if len(calls) == 2 else -0.5}

        result = run_engineering_sweep(definition, evaluate, 0)
        self.assertEqual(result["max_evaluations"], 2)
        self.assertEqual(result["evaluations_used"], 2)
        self.assertEqual(result["first_success_evaluation"], 2)
        self.assertEqual(result["stop_reason"], "success")
        immediate = run_engineering_sweep(definition, lambda action: {"status": "ok", "success": True}, 0)
        self.assertEqual(immediate["evaluations_used"], 1)
        self.assertEqual(immediate["best_reward"], 1.0)

    def test_invalid_rewards_match_existing_baseline_contract(self):
        for reward in (math.nan, math.inf, True, 0.1, -2, "bad"):
            with self.subTest(reward=reward):
                result = run_engineering_sweep(task(), lambda action: {**failed(action), "reward": reward}, 0, budget=1)
                self.assertEqual(result["best_reward"], -1.0)

    def test_callback_cannot_mutate_actions_task_or_prior_observations(self):
        definition = task()
        shared = failed({})

        def evaluate(action):
            action.clear()
            return shared

        result = run_engineering_sweep(definition, evaluate, 0, budget=2)
        shared["metrics"].clear()
        self.assertEqual(result["trajectory"][0]["parameters"], definition["initial_parameters"])
        self.assertEqual(result["trajectory"][0]["metrics"], {"distance": 0.5})
        self.assertEqual(definition, task())

    def test_private_task_fields_are_never_read_or_copied(self):
        class Private:
            def __deepcopy__(self, memo):
                raise AssertionError("Private material accessed")

        definition = task()
        definition.update(reference=Private(), design_context=Private(), source=Private(),
                          characterization_reference=Private(), start_variant=Private())
        self.assertEqual(run_engineering_sweep(definition, failed, 0), run_engineering_sweep(task(), failed, 0))

    def test_infrastructure_failures_and_malformed_callback_propagate(self):
        def unavailable(action):
            raise RuntimeError("worker unavailable")

        with self.assertRaisesRegex(RuntimeError, "worker unavailable"):
            run_engineering_sweep(task(), unavailable, 0)
        with self.assertRaisesRegex(ValueError, "result dictionary"):
            run_engineering_sweep(task(), lambda action: None, 0)

    def test_invalid_configuration_rejected_before_evaluation(self):
        def unexpected(action):
            raise AssertionError("An invalid task must not be evaluated")

        for value in (0, -1, True, 1.5):
            with self.subTest(budget=value), self.assertRaises(ValueError):
                run_engineering_sweep(task(), unexpected, 0, budget=value)
        for seed in (True, 1.5, "seed"):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                run_engineering_sweep(task(), unexpected, seed)
        for initial in (0, math.inf, True):
            definition = task()
            definition["initial_parameters"]["C"] = initial
            with self.subTest(initial=initial), self.assertRaises(ValueError):
                run_engineering_sweep(definition, unexpected, 0)
        with self.assertRaisesRegex(ValueError, "callable"):
            run_engineering_sweep(task(), None, 0)


if __name__ == "__main__":
    unittest.main()
