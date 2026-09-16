"""Bridge release tasks into the existing catalog without starting a worker."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.simulator import ROOT, digest
from benchmark.worker_catalog import export_worker_catalog
from training.catalog import group_key, load_catalog


class WorkerCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="worker_catalog_test_", dir=ROOT / "runs")
        self.bundle = Path(self.temporary.name)
        (self.bundle / "tasks").mkdir()
        self.rows, self.tasks = [], {}
        for number, topology, family, split in ((1, "alpha", "family_a", "train"),
                                                 (2, "beta", "family_a", "train"),
                                                 (3, "gamma", "family_b", "test")):
            name = "worker_fixture_" + str(number)
            task = {"id": name, "circuit_directory": "circuits/" + topology, "max_evaluations": 30,
                    "design_context": {"topology": topology, "topology_family": family}}
            path = self.bundle / "tasks" / (name + ".json")
            path.write_text(json.dumps(task))
            self.tasks[name] = task
            self.rows.append({"id": name, "path": "tasks/" + name + ".json", "sha256": digest(path),
                              "topology": topology, "topology_family": family, "split": split,
                              "difficulty": "easy", "reference_path": "private/never_read.json",
                              "reference_sha256": "secret-value-not-used"})
        self.index = {"schema_version": 1, "split_policy": "topology_family", "task_count": 3,
                      "status": "awaiting_independent_release_verification", "tasks": self.rows}
        self.index_path = self.bundle / "index.json"
        self.write_index()

    def tearDown(self):
        self.temporary.cleanup()

    def write_index(self):
        self.index_path.write_text(json.dumps(self.index))

    def test_export_loads_unchanged_numeric_catalog_and_preserves_all_splits(self):
        with patch("training.catalog.require_training_approval", side_effect=AssertionError("approval must not be invoked")):
            output = export_worker_catalog(self.index_path)
            manifest, records = load_catalog(output, training=False)
        self.assertEqual(output, self.bundle / "worker_catalog.json")
        self.assertEqual(manifest["split_policy"], "topology")
        self.assertTrue(manifest["family_holdout_preserved"])
        self.assertEqual(manifest["family_splits"], {"family_a": "train", "family_b": "test"})
        self.assertEqual(manifest["source_index_sha256"], digest(self.index_path))
        self.assertFalse(manifest["compatibility"]["benchmark_episode_context_active"])
        self.assertIn("Not granted", manifest["training_approval"])
        self.assertEqual(set(records), set(self.tasks))
        for entry, original in zip(manifest["tasks"], self.rows):
            task_path = self.bundle / original["path"]
            self.assertEqual(entry["path"], str(task_path.relative_to(ROOT)))
            self.assertEqual(entry["split"], original["split"])
            self.assertEqual(entry["sha256"], original["sha256"])
            self.assertEqual(entry["group"], group_key(self.tasks[entry["id"]], "topology"))
            self.assertFalse(any(key.startswith("reference") for key in entry))
        self.assertFalse((self.bundle / "private").exists())

    def test_family_holdout_violation_is_rejected_before_output_creation(self):
        self.rows[1]["split"] = "validation"
        self.write_index()
        with self.assertRaisesRegex(ValueError, "family occurs in different splits"):
            export_worker_catalog(self.index_path)
        self.assertFalse((self.bundle / "worker_catalog.json").exists())

    def test_coarser_worker_topology_group_must_also_stay_in_one_split(self):
        name = self.rows[2]["id"]
        self.tasks[name]["circuit_directory"] = "circuits/alpha"
        path = self.bundle / self.rows[2]["path"]
        path.write_text(json.dumps(self.tasks[name]))
        self.rows[2]["sha256"] = digest(path)
        self.write_index()
        with self.assertRaisesRegex(ValueError, "worker's topology group"):
            export_worker_catalog(self.index_path)
        self.assertFalse((self.bundle / "worker_catalog.json").exists())

    def test_task_tampering_and_index_identity_errors_fail_before_output(self):
        original = deepcopy(self.index)
        mutations = [lambda: self.rows[0].update(sha256="0" * 64),
                     lambda: self.rows[0].update(id=self.rows[1]["id"]),
                     lambda: self.rows[0].update(path="../escaped.json"),
                     lambda: self.rows[0].update(split="smoke"),
                     lambda: self.index.update(task_count=250),
                     lambda: self.index.update(split_policy="requirements")]
        for mutate in mutations:
            self.index = deepcopy(original)
            self.rows = self.index["tasks"]
            mutate()
            self.write_index()
            with self.subTest(index=self.index), self.assertRaises(ValueError):
                export_worker_catalog(self.index_path)
            self.assertFalse((self.bundle / "worker_catalog.json").exists())

    def test_task_context_cannot_silently_change_the_declared_family(self):
        name = self.rows[0]["id"]
        self.tasks[name]["design_context"]["topology_family"] = "changed_family"
        path = self.bundle / self.rows[0]["path"]
        path.write_text(json.dumps(self.tasks[name]))
        self.rows[0]["sha256"] = digest(path)
        self.write_index()
        with self.assertRaisesRegex(ValueError, "context disagrees"):
            export_worker_catalog(self.index_path)

    def test_symlink_outside_bundle_is_not_accepted_as_an_indexed_task(self):
        path = self.bundle / self.rows[0]["path"]
        path.unlink()
        path.symlink_to(ROOT / "benchmark/domains.json")
        self.rows[0]["sha256"] = digest(path)
        self.write_index()
        with self.assertRaisesRegex(ValueError, "escapes its bundle"):
            export_worker_catalog(self.index_path)

    def test_existing_output_is_preserved_and_alternate_new_output_is_supported(self):
        output = self.bundle / "worker_catalog.json"
        output.write_text("existing artifact")
        with self.assertRaises(FileExistsError):
            export_worker_catalog(self.index_path)
        self.assertEqual(output.read_text(), "existing artifact")
        alternate = self.bundle / "worker_catalog_v2.json"
        self.assertEqual(export_worker_catalog(self.index_path, alternate), alternate)
        self.assertEqual(len(load_catalog(alternate, training=False)[1]), 3)


if __name__ == "__main__":
    unittest.main()
