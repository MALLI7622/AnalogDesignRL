from copy import deepcopy
import math
import random
import unittest

from benchmark.baselines import METHODS, run_search


def task():
    return {"id": "synthetic", "max_evaluations": 30,
            "initial_parameters": {"C": 1e-12, "I": 1e-6, "M": 2},
            "parameters": {"C": {"min": 1e-12, "max": 1e-9, "unit": "F"},
                           "I": {"min": 1e-6, "max": 1e-3, "unit": "A"},
                           "M": {"min": 1, "max": 8, "integer": True, "unit": "multiplicity"}}}


def failed(action):
    return {"status": "ok", "success": False, "reward": -0.5,
            "metrics": {"measurement": 0.5}, "checks": {"measurement": False}}


class BenchmarkBaselineTests(unittest.TestCase):
    def test_reproducible_full_actions_respect_mixed_bounds_and_budget(self):
        original = task()
        for method in METHODS:
            with self.subTest(method=method):
                calls = []

                def evaluate(action):
                    calls.append(deepcopy(action))
                    return failed(action)

                first = run_search(original, evaluate, method, 7, budget=12)
                second = run_search(original, failed, method, 7, budget=12)
                self.assertEqual(first, second)
                self.assertEqual(len(calls), 12)
                self.assertEqual(first["evaluations_used"], 12)
                self.assertEqual(calls[0], original["initial_parameters"])
                self.assertEqual(len({tuple(sorted(x.items())) for x in calls}), 12)
                for action in calls:
                    self.assertEqual(set(action), set(original["parameters"]))
                    for name, value in action.items():
                        rule = original["parameters"][name]
                        self.assertGreaterEqual(value, rule["min"])
                        self.assertLessEqual(value, rule["max"])
                    self.assertIsInstance(action["M"], int)
                self.assertEqual(first["stop_reason"], "budget_exhausted")
        self.assertEqual(original, task())

    def test_stops_immediately_at_success_and_caps_to_task_budget(self):
        definition = task()
        definition["max_evaluations"] = 2
        for method in METHODS:
            with self.subTest(method=method):
                observations = []

                def evaluate(action):
                    observations.append(action)
                    return {**failed(action), "success": len(observations) == 2, "reward": 1.0}

                result = run_search(definition, evaluate, method, 3, budget=30)
                self.assertEqual(len(observations), 2)
                self.assertTrue(result["success"])
                self.assertEqual(result["first_success_evaluation"], 2)
                self.assertEqual(result["max_evaluations"], 2)
                self.assertEqual(result["stop_reason"], "success")
                self.assertEqual(len(run_search(definition, lambda action: {
                    "status": "ok", "success": True, "reward": 1}, method, 0)["trajectory"]), 1)

    def test_failed_simulations_count_and_cannot_succeed_with_positive_reward(self):
        for method in METHODS:
            result = run_search(task(), lambda action: {
                "status": "timeout", "success": True, "reward": 1,
                "metrics": {}, "checks": {}}, method, 0, budget=4)
            self.assertFalse(result["success"])
            self.assertEqual(result["evaluations_used"], 4)
            self.assertTrue(all(item["reward"] == -1 for item in result["trajectory"]))

    def test_tiny_integer_domain_exhausts_without_duplicate_calls(self):
        definition = {"initial_parameters": {"M": 1}, "max_evaluations": 30,
                      "parameters": {"M": {"min": 1, "max": 3, "integer": True}}}
        for method in METHODS:
            result = run_search(definition, failed, method, 11)
            self.assertEqual(result["evaluations_used"], 3)
            self.assertEqual(result["stop_reason"], "no_unique_candidate")
            self.assertEqual({item["parameters"]["M"] for item in result["trajectory"]}, {1, 2, 3})

    def test_coordinate_search_uses_feedback_to_solve_log_scale_objective(self):
        definition = {"id": "log_scale", "max_evaluations": 30,
                      "initial_parameters": {"C": 1e-12},
                      "parameters": {"C": {"min": 1e-12, "max": 1e-8, "unit": "F"}}}

        def evaluate(action):
            error = abs(math.log10(action["C"]) + 10)
            return {"status": "ok", "success": error < 1e-8,
                    "reward": 1 if error < 1e-8 else -error / 4,
                    "metrics": {"distance": error}, "checks": {"distance": error < 1e-8}}

        result = run_search(definition, evaluate, "coordinate_search", 0)
        self.assertTrue(result["success"])
        self.assertLessEqual(result["evaluations_used"], 5)
        self.assertAlmostEqual(result["best_parameters"]["C"] / 1e-10, 1)

    def test_random_log_sampling_differs_from_linear(self):
        definition = task()
        linear = run_search(definition, failed, "uniform_linear", 0, budget=2)
        logarithmic = run_search(definition, failed, "uniform_log", 0, budget=2)
        self.assertNotEqual(linear["trajectory"][1]["parameters"]["C"],
                            logarithmic["trajectory"][1]["parameters"]["C"])

    def test_explicit_log_scale_applies_to_resistance_for_both_searches(self):
        definition = {"id": "resistor", "max_evaluations": 30,
                      "initial_parameters": {"R": 187500.0},
                      "parameters": {"R": {"min": 187500.0, "max": 3000000.0,
                                            "unit": "ohm", "sampling_scale": "log"}}}
        logarithmic = run_search(definition, failed, "uniform_log", 0, budget=2)
        expected = math.exp(random.Random(0).uniform(math.log(187500), math.log(3000000)))
        self.assertAlmostEqual(logarithmic["trajectory"][1]["parameters"]["R"] / expected, 1)
        linear = run_search(definition, failed, "uniform_linear", 0, budget=2)
        self.assertNotEqual(linear["trajectory"][1]["parameters"]["R"],
                            logarithmic["trajectory"][1]["parameters"]["R"])

        def evaluate(action):
            distance = abs(math.log(action["R"] / 750000) / math.log(16))
            return {"status": "ok", "success": distance < 1e-8,
                    "reward": 1 if distance < 1e-8 else -distance}

        result = run_search(definition, evaluate, "coordinate_search", 0)
        self.assertTrue(result["success"])
        self.assertLessEqual(result["evaluations_used"], 5)

    def test_explicit_log_does_not_log_integer_or_nonpositive_bounds(self):
        definition = {"initial_parameters": {"M": 2, "V": 0.0},
                      "parameters": {"M": {"min": 1, "max": 8, "integer": True,
                                            "sampling_scale": "log"},
                                     "V": {"min": -1.0, "max": 1.0,
                                            "sampling_scale": "log"}}}
        linear = run_search(definition, failed, "uniform_linear", 0, budget=4)
        logarithmic = run_search(definition, failed, "uniform_log", 0, budget=4)
        self.assertEqual(linear["trajectory"], logarithmic["trajectory"])

    def test_callback_mutation_does_not_modify_task_or_recorded_action(self):
        definition = task()

        def evaluate(action):
            action.clear()
            return failed(action)

        result = run_search(definition, evaluate, "uniform_linear", 0, budget=1)
        self.assertEqual(result["trajectory"][0]["parameters"], definition["initial_parameters"])
        self.assertEqual(definition, task())

    def test_infrastructure_exception_propagates_and_invalid_configuration_rejects(self):
        def unavailable(action):
            raise RuntimeError("worker unavailable")

        with self.assertRaisesRegex(RuntimeError, "worker unavailable"):
            run_search(task(), unavailable, "uniform_linear", 0)
        for budget in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                run_search(task(), failed, "uniform_linear", 0, budget=budget)
        with self.assertRaises(ValueError):
            run_search(task(), failed, "unknown", 0)
        with self.assertRaises(ValueError):
            run_search(task(), failed, "uniform_linear", True)


if __name__ == "__main__":
    unittest.main()
