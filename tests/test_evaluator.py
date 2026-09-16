import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from analog_design.metrics import ac_metrics, read_table, score, settling_time, transient_metrics
from analog_design.simulator import ROOT, evaluate, parameters_for


class MetricTests(unittest.TestCase):
    def test_single_pole_amplifier_against_analytic_solution(self):
        gain, pole = 1000, 1000
        rows = []
        for index in range(1001):
            frequency = 10 ** (-1 + index / 100)
            ratio = frequency / pole
            rows.append([frequency, 20 * math.log10(gain / math.sqrt(1 + ratio * ratio)),
                         -math.atan(ratio)])
        measured = ac_metrics(rows)
        expected_unity = pole * math.sqrt(gain * gain - 1)
        self.assertAlmostEqual(measured["unity_gain_hz"] / expected_unity, 1, places=5)
        expected_margin = 180 - math.degrees(math.atan(expected_unity / pole))
        self.assertAlmostEqual(measured["phase_margin_deg"], expected_margin, places=4)

    def test_missing_or_multiple_unity_crossings_fail(self):
        for rows in ([[1, 20, 0], [10, 10, -1]],
                     [[1, 20, 0], [10, -10, -1], [100, 10, -2]]):
            with self.assertRaises(ValueError):
                ac_metrics(rows)

    def test_negative_gain_is_not_made_positive(self):
        result = score({"gain_db": -60}, {"gain_db": {"min": 50}})
        self.assertFalse(result["success"])

    def test_missing_and_nonfinite_measurements_fail(self):
        for metrics in ({}, {"gain_db": float("nan")}, {"gain_db": float("inf")}):
            self.assertFalse(score(metrics, {"gain_db": {"min": 50}})["success"])
        with self.assertRaises(ValueError):
            score({}, {})

    def test_every_constraint_must_pass(self):
        self.assertFalse(score({"gain_db": 80, "power_w": 0.003},
                               {"gain_db": {"min": 50}, "power_w": {"max": 0.002}})["success"])

    def test_settling_uses_last_excursion(self):
        output = [0, 0.8, 1, 1, 1.2, 1, 1, 1, 1, 1]
        rows = [[i, 1, value] for i, value in enumerate(output)]
        self.assertEqual(settling_time(rows, 0, 10, 1, 0.01, 2), 5)

    def test_late_crossing_without_hold_time_fails(self):
        rows = [[i, 1, 0 if i < 9 else 1] for i in range(10)]
        with self.assertRaises(ValueError):
            settling_time(rows, 0, 10, 1, 0.01, 2)

    def test_incomplete_transient_fails(self):
        test = json.loads((ROOT / "tasks/fan_smc_nominal.json").read_text())["transient"]
        with self.assertRaises(ValueError):
            transient_metrics([[i * 1e-6, 0.3, 0.3, 0] for i in range(10)], test)

    def test_corrupt_table_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.tsv"
            for content in ("", "frequency gain phase\n", "frequency gain phase\n1 nan 0\n"):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    read_table(path, 3)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.task_path = ROOT / "tasks/fan_smc_nominal.json"
        self.task = json.loads(self.task_path.read_text())

    def test_invalid_actions_are_rejected(self):
        for values in ({"supply_v": 5}, {"CAPACITOR_0": float("nan")},
                       {"CAPACITOR_0": "5p\n.end"}, {"CURRENT_0_BIAS": -1},
                       {"MOSFET_23_1_M_gm3_NMOS": 4.5}):
            with self.assertRaises(ValueError):
                parameters_for(self.task, values)

    def test_invalid_action_does_not_launch_simulator(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("analog_design.simulator.platform.platform", return_value="test-host"), \
             patch("analog_design.simulator.subprocess.run") as run:
            result = evaluate(self.task_path, {"CAPACITOR_0": -1}, Path(temporary) / "run")
            self.assertFalse(result["success"])
            self.assertEqual(result["reward"], -1)
            run.assert_not_called()

    def test_existing_output_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileExistsError):
                evaluate(self.task_path, {}, temporary)

    def test_timeout_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("analog_design.simulator.platform.platform", return_value="test-host"), \
             patch("analog_design.simulator.write_inputs", return_value=(ROOT / "circuits/fan_smc", self.task_path)), \
             patch("analog_design.simulator.shutil.which", return_value="ngspice"), \
             patch("analog_design.simulator.subprocess.check_output", return_value="ngspice-47 "), \
             patch("analog_design.simulator.digest", return_value="test-hash"), \
             patch("analog_design.simulator.subprocess.run", side_effect=subprocess.TimeoutExpired("ngspice", 1)):
            result = evaluate(self.task_path, {}, Path(temporary) / "run")
            self.assertEqual(result["status"], "timeout")
            self.assertFalse(result["success"])
            self.assertEqual(result["reward"], -1)

    def test_constraint_disagreement_is_rejected_even_when_both_scores_fail(self):
        # Power fails in both paths, so comparing only overall success would
        # conceal an ambiguous phase-margin requirement and emit a valid score.
        for primary_margin, native_margin in ((59.99, 60.01), (60.01, 59.99)):
            with self.subTest(primary_margin=primary_margin), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "run"
                native = {"gain_db": 60.0, "unity_gain_hz": 2e6,
                          "phase_margin_deg": native_margin, "power_w": 0.0036,
                          "dc_error_v": 0.0, "max_tracking_error_v": 0.001,
                          "settling_rise_s": 5e-7, "settling_fall_s": 5e-7}

                def run_simulator(command, *, cwd, **kwargs):
                    (Path(cwd) / command[-1].replace(".cir", ".log")).write_text("")
                    return subprocess.CompletedProcess(command, 0, "", "")

                def read_output(path, columns):
                    if path.name == "operating_point.tsv":
                        return [[0, 0.3, -0.002]]
                    return [[0.1, 60, 0], [1e9, -20, -2]]

                with patch.multiple("analog_design.simulator",
                                    write_inputs=lambda *args: (ROOT / "circuits/fan_smc", self.task_path),
                                    digest=lambda path: "test-hash",
                                    read_table=read_output,
                                    ac_metrics=lambda rows: {"gain_db": 60.0, "unity_gain_hz": 2e6,
                                                             "phase_margin_deg": primary_margin},
                                    transient_metrics=lambda rows, test: {
                                        "max_tracking_error_v": 0.001,
                                        "settling_rise_s": 5e-7, "settling_fall_s": 5e-7},
                                    all_measurements=lambda *args: native), \
                     patch("analog_design.simulator.platform.platform", return_value="test-host"), \
                     patch("analog_design.simulator.shutil.which", return_value="ngspice"), \
                     patch("analog_design.simulator.subprocess.check_output", return_value="ngspice-47 "), \
                     patch("analog_design.simulator.subprocess.run", side_effect=run_simulator):
                    result = evaluate(self.task_path, {}, directory)

                self.assertTrue(result["crosscheck"]["agrees"])
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["success"])
                self.assertEqual(result["reward"], -1)
                self.assertIn("disagree on pass/fail", result["error"])


if __name__ == "__main__":
    unittest.main()
