import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.episode import Episode
from analog_design.simulator import ROOT


def make_episode(directory, budget=2):
    task = json.loads((ROOT / "tasks/fan_smc_sizing.json").read_text())
    task["max_evaluations"] = budget
    task_path = Path(directory) / "definition.json"
    task_path.write_text(json.dumps(task))
    return Episode(task_path, Path(directory) / "episode")


class EpisodeTests(unittest.TestCase):
    def test_invalid_actions_spend_budget_and_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("analog_design.simulator.platform.platform", return_value="test-host"), \
             patch("analog_design.simulator.subprocess.run") as simulator:
            episode = make_episode(directory)
            episode.step({"CAPACITOR_0": float("nan")})
            result = episode.step({"unknown_parameter": 1})
            self.assertTrue(result["truncated"])
            self.assertFalse(result["terminated"])
            self.assertEqual(result["evaluations_remaining"], 0)
            self.assertEqual(episode.parameters, episode.task["initial_parameters"])
            with self.assertRaises(RuntimeError):
                episode.step({})
            self.assertEqual(len(episode.history), 2)
            json.loads((episode.directory / "episode.json").read_text())
            simulator.assert_not_called()

    def test_partial_actions_use_current_design_and_success_ends_episode(self):
        with tempfile.TemporaryDirectory() as directory:
            episode = make_episode(directory, budget=3)
            candidates = []

            def fake_evaluation(task, parameters, run_dir, timeout):
                candidates.append(dict(parameters))
                success = len(candidates) == 2
                return {"task_id": "test", "status": "ok", "success": success,
                        "reward": 1 if success else -0.2, "metrics": {}, "checks": {},
                        "simulator_invocations": 2, "elapsed_s": 0,
                        "parameters": parameters}

            with patch("analog_design.episode.evaluate", side_effect=fake_evaluation):
                episode.step({"CAPACITOR_0": 8e-12})
                result = episode.step({"CURRENT_0_BIAS": 5e-5})
                self.assertEqual(candidates[-1]["CAPACITOR_0"], 8e-12)
                self.assertTrue(result["terminated"])
                self.assertFalse(result["truncated"])
                self.assertEqual(result["evaluations_remaining"], 1)
                with self.assertRaises(RuntimeError):
                    episode.step({})

    def test_specification_does_not_expose_reference_files_or_mutable_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            episode = make_episode(directory)
            specification = episode.specification()
            self.assertNotIn("circuit_directory", specification)
            specification["parameters"]["CAPACITOR_0"]["min"] = -1
            self.assertGreater(episode.specification()["parameters"]["CAPACITOR_0"]["min"], 0)


if __name__ == "__main__":
    unittest.main()
