import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.crosscheck import ac_measurements, compare, measured_value
from analog_design.author_reference import render_deck
from analog_design.episode import Episode
from analog_design.metrics import ac_metrics, score, transient_metrics
from analog_design.simulator import ROOT


class CrosscheckTests(unittest.TestCase):
    def test_native_measurements_require_one_finite_value(self):
        for log in ("", "check = nan", "check = inf", "check = 1\ncheck = 2", "check = failed"):
            with self.assertRaises(ValueError):
                measured_value(log, "check")
        self.assertEqual(measured_value("check = 6.1e1 when=1e6", "check"), 61)

    def test_missing_native_metric_cannot_be_used_as_agreement(self):
        with self.assertRaises(ValueError):
            ac_measurements("check_gain_db_result = 50")
        self.assertFalse(compare({"gain_db": 50}, {})["agrees"])

    def test_measurement_disagreement_is_detected(self):
        self.assertFalse(compare({"gain_db": 60}, {"gain_db": 65})["agrees"])
        with self.assertRaises(ValueError):
            compare({"gain_db": 60}, {"gain_db": float("nan")})

    def test_boolean_is_not_a_performance_measurement(self):
        self.assertFalse(score({"gain": True}, {"gain": {"min": 1}})["success"])

    def test_ambiguous_or_boundary_unity_crossings_are_rejected(self):
        for rows in ([[1, 20, 0], [10, 0, -1]],
                     [[1, 20, 0], [10, 0, -1], [100, 1, -1.2], [1000, -20, -2]],
                     [[1, 20, 0], [10, 0, -1], [100, 0, -1.2], [1000, -20, -2]]):
            with self.assertRaises(ValueError):
                ac_metrics(rows)

    def test_missing_middle_of_transient_is_rejected(self):
        test = json.loads((ROOT / "tasks/fan_smc_nominal.json").read_text())["transient"]
        rows = [[i * 5e-6, 0.3, 0.3, 0] for i in range(11)]
        with self.assertRaisesRegex(ValueError, "gap"):
            transient_metrics(rows, test)

    def test_training_requires_review_before_any_episode_is_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "episode"
            with self.assertRaisesRegex(RuntimeError, "Training is not approved"):
                Episode(ROOT / "tasks/fan_smc_sizing.json", output, for_training=True)
            self.assertFalse(output.exists())

    def test_author_fixture_parameters_cannot_inject_spice(self):
        source = '.include "/source/45nm_bulk.txt"\n.param mp1=10 mn1=38 cc=3p\n.ac dec 10 1 10G\n'
        for parameters in ({"mp1": "1\n.end"}, {"mp1": 100}, {"mn1": 1.5},
                           {"cc": 1.05e-12}, {"not_editable": 1}):
            with patch("analog_design.author_reference.Path.read_text", return_value=source), self.assertRaises(ValueError):
                render_deck({"id": "test", "parameters": parameters})


if __name__ == "__main__":
    unittest.main()
