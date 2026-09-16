from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark import simulation


class BenchmarkCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.circuit = self.root / "circuits/example"
        self.circuit.mkdir(parents=True)
        (self.circuit / "netlist.spice").write_text("* fixed topology\n")
        (self.circuit / "reference.params").write_text(".param W=2\n")
        self.corner = self.root / ".deps/sky130_pdk/libs.tech/ngspice/corners/tt.spice"
        self.corner.parent.mkdir(parents=True)
        self.corner.write_text('.include "device.spice"\n')
        self.model = self.corner.with_name("device.spice")
        self.model.write_text(".model mos nmos level=1 kp=1e-4\n")
        for name in ("analog_design/simulator.py", "analog_design/metrics.py",
                     "analog_design/crosscheck.py", "analog_design/episode.py",
                     "benchmark/simulation.py", "dependencies.lock.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.executable = self.root / "ngspice"
        self.executable.write_bytes(b"simulator binary version 47")
        self.version = "ngspice-47 test"
        self.calls = 0
        self.task = {
            "id": "example", "circuit_directory": "circuits/example", "subcircuit": "example",
            "conditions": {"corner": "tt", "supply_v": 1.8},
            "parameters": {"W": {"min": 1, "max": 5}}, "initial_parameters": {"W": 2},
            "ac": {"points_per_decade": 100}, "transient": {"max_step_s": 1e-9},
            "constraints": {"gain_db": {"min": 50}},
        }
        for patcher in (patch.object(simulation, "ROOT", self.root),
                        patch.object(simulation.platform, "platform", return_value="test-host"),
                        patch.object(simulation.shutil, "which", return_value=str(self.executable)),
                        patch.object(simulation.subprocess, "check_output", side_effect=lambda *a, **k: self.version),
                        patch.object(simulation, "evaluate", side_effect=self.fake_evaluate)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_evaluate(self, task_path, parameters, run_dir, timeout):
        self.calls += 1
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "ac.tsv").write_bytes(b"frequency gain\n1 60\n")
        result = {"status": "ok", "success": True, "reward": 1.0, "checks": {},
                  "parameters": parameters, "metrics": {"gain_db": 60, "power_w": 0.01},
                  "independent_metrics": {"gain_db": 60, "power_w": 0.01}}
        simulation.save_json(run_dir / "result.json", result)
        return result

    def cache(self, timeout=30):
        return simulation.MeasurementCache(self.root / "runs/cache", simulator_timeout=timeout)

    def test_targets_are_rescored_without_extra_physical_evaluations(self):
        cache = self.cache()
        easy = cache.evaluate(self.task, {})
        hard_task = deepcopy(self.task)
        hard_task["constraints"]["gain_db"]["min"] = 70
        hard = cache.evaluate(hard_task, {})
        self.assertTrue(easy["success"])
        self.assertFalse(hard["success"])
        self.assertEqual(easy["measurement_key"], hard["measurement_key"])
        self.assertEqual(self.calls, 1)
        self.assertEqual(cache.physical_evaluations, 1)

    def test_rescore_checks_every_constraint_in_both_paths(self):
        result = {"status": "ok", "metrics": {"gain_db": 59.99, "power_w": 1},
                  "independent_metrics": {"gain_db": 60.01, "power_w": 1}}
        scored = simulation.rescore(result, {"gain_db": {"min": 60}, "power_w": {"max": 0.1}})
        self.assertEqual(scored["status"], "failed")
        self.assertEqual(scored["reward"], -1)
        self.assertIn("disagree", scored["error"])

    def test_persisted_reuse_validates_identity_and_preserves_lossless_archive(self):
        result = self.cache().evaluate(self.task, {})
        directory = self.root / result["measurement_directory"]
        self.assertFalse((directory / "ac.tsv").exists())
        with gzip.open(directory / "ac.tsv.gz", "rb") as archive:
            self.assertEqual(archive.read(), b"frequency gain\n1 60\n")
        second = self.cache().evaluate(self.task, {})
        self.assertEqual(result["measurement_key"], second["measurement_key"])
        self.assertEqual(self.calls, 1)
        entry = json.loads((directory / "cache_entry.json").read_text())
        self.assertEqual(entry["identity"]["runtime"]["ngspice_version"], self.version)

    def test_changed_inputs_abort_live_reuse_and_get_new_identity(self):
        paths = [self.circuit / "netlist.spice", self.circuit / "reference.params", self.model,
                 self.root / "analog_design/metrics.py", self.executable]
        for path in paths:
            with self.subTest(path=path):
                cache = self.cache()
                before = cache.evaluate(self.task, {})
                path.write_bytes(path.read_bytes() + b"\n* changed\n")
                with self.assertRaisesRegex(RuntimeError, "input changed"):
                    cache.evaluate(self.task, {})
                with self.assertRaisesRegex(RuntimeError, "input changed"):
                    cache.assert_unchanged()
                after = self.cache().evaluate(self.task, {})
                self.assertNotEqual(before["measurement_key"], after["measurement_key"])

    def test_changed_version_changes_key_and_fails_stage_boundary(self):
        cache = self.cache()
        before = cache.evaluate(self.task, {})
        self.version = "ngspice-48 test"
        with self.assertRaisesRegex(RuntimeError, "version changed"):
            cache.assert_unchanged()
        after = self.cache().evaluate(self.task, {})
        self.assertNotEqual(before["measurement_key"], after["measurement_key"])

    def test_changed_timeout_cannot_reuse_a_previous_result(self):
        before = self.cache(15).evaluate(self.task, {})
        after = self.cache(30).evaluate(self.task, {})
        self.assertNotEqual(before["measurement_key"], after["measurement_key"])
        self.assertEqual(self.calls, 2)

    def test_timeout_must_be_finite_and_positive(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.cache(timeout)

    def test_physical_task_changes_get_distinct_keys(self):
        cache = self.cache()
        keys = {cache.evaluate(self.task, {})["measurement_key"]}
        for field, value in (("conditions", {"corner": "tt", "supply_v": 1.7}),
                             ("parameters", {"W": {"min": 1, "max": 6}}),
                             ("ac", {"points_per_decade": 200}),
                             ("transient", {"max_step_s": 2e-9})):
            task = deepcopy(self.task)
            task[field] = value
            keys.add(cache.evaluate(task, {})["measurement_key"])
        self.assertEqual(len(keys), 5)

    def test_modified_persisted_result_is_not_trusted(self):
        result = self.cache().evaluate(self.task, {})
        path = self.root / result["measurement_directory"] / "result.json"
        data = json.loads(path.read_text())
        data["metrics"]["gain_db"] = 100
        simulation.save_json(path, data)
        with self.assertRaisesRegex(ValueError, "hash disagrees"):
            self.cache().evaluate(self.task, {})

    def test_worker_exception_is_repeated_to_waiters(self):
        cache = self.cache()
        with patch.object(simulation, "evaluate", side_effect=RuntimeError("worker exploded")):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(cache.measure, self.task, {}) for _ in range(2)]
                for future in futures:
                    with self.assertRaisesRegex(RuntimeError, "worker exploded"):
                        future.result()

    def test_input_changed_during_simulation_does_not_get_published(self):
        cache = self.cache()

        def changing_simulator(*args):
            result = self.fake_evaluate(*args)
            self.model.write_text("* model changed during simulation\n")
            return result

        with patch.object(simulation, "evaluate", side_effect=changing_simulator):
            with self.assertRaisesRegex(RuntimeError, "input changed"):
                cache.measure(self.task, {})
        self.assertEqual(list((self.root / "runs/cache").rglob("cache_entry.json")), [])

    def test_include_closure_tracks_nested_models_and_rejects_dynamic_paths(self):
        nested = self.corner.with_name("nested.spice")
        self.model.write_text('.include "nested.spice"\n')
        nested.write_text('.lib tt\n.model m nmos\n.endl tt\n')
        identity = simulation.measurement_identity(self.task)
        self.assertIn(str(nested), identity["spice_dependency_sha256"])
        self.model.write_text('.include "$UNTRACKED/device.spice"\n')
        with self.assertRaisesRegex(ValueError, "Dynamic SPICE include"):
            simulation.measurement_identity(self.task)


if __name__ == "__main__":
    unittest.main()
