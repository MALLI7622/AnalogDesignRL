"""Check the transfer payload, source bindings, and unchanged qualification gate."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.simulator import ROOT
from scripts.prepare_training_bundle import prepare_training_bundle
from training.catalog import group_key, load_catalog


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TrainingBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.bundle = self.root / "datasets/analog_benchmark_250_v1"
        (self.bundle / "tasks").mkdir(parents=True)
        self.output = self.root / "transfer"
        template = json.loads((ROOT / "tasks/autockt_two_stage_sizing.json").read_text())
        self.rows, catalog_rows = [], []
        for n, split in enumerate(["train"] * 174 + ["validation"] * 21 + ["test"] * 55):
            name = f"task_{n:03d}"
            task = deepcopy(template)
            task.update(id=name, circuit_directory="circuits/" + split, max_evaluations=30)
            path = self.bundle / "tasks" / (name + ".json")
            path.write_text(json.dumps(task, indent=2) + "\n")
            row = {"id": name, "path": "tasks/" + path.name, "sha256": sha(path), "split": split,
                   "topology": split, "topology_family": "family_" + split, "difficulty": "easy",
                   "reference_path": "private/answer-not-present.json", "reference_sha256": "never-read"}
            self.rows.append(row)
            catalog_rows.append({k: row[k] for k in ("id", "sha256", "split", "topology", "topology_family", "difficulty")})
            catalog_rows[-1].update(path=str(path.relative_to(self.root)), group=group_key(task, "topology"))
        self.index = {"schema_version": 1, "task_count": 250, "split_policy": "topology_family", "tasks": self.rows}
        self.index_path = self.bundle / "index.json"
        self.catalog_path = self.bundle / "worker_catalog.json"
        self.catalog = {"schema_version": 1, "split_policy": "topology", "tasks": catalog_rows,
                        "source_index": str(self.index_path.relative_to(self.root)),
                        "source_split_policy": "topology_family", "family_holdout_preserved": True,
                        "family_splits": {"family_" + s: s for s in ("train", "validation", "test")}}
        self.write_sources()
        self.root_patch = patch("scripts.prepare_training_bundle.ROOT", self.root)
        self.loader_patch = patch("training.catalog.ROOT", self.root)
        self.root_patch.start()
        self.loader_patch.start()

    def tearDown(self):
        self.loader_patch.stop()
        self.root_patch.stop()
        self.temp.cleanup()

    def write_sources(self):
        self.index_path.write_text(json.dumps(self.index))
        self.catalog["source_index_sha256"] = sha(self.index_path)
        self.catalog_path.write_text(json.dumps(self.catalog))

    def prepare(self):
        return prepare_training_bundle(self.index_path, self.catalog_path, self.output)

    def test_exact_public_payload_excludes_tests_and_loads_after_relocation(self):
        before = {p: sha(p) for p in self.bundle.rglob("*.json")}
        with patch("training.catalog.require_training_approval", side_effect=AssertionError("must not approve")):
            result = self.prepare()
        self.assertEqual(result["task_count"], 195)
        self.assertEqual(result["split_counts"], {"train": 174, "validation": 21})
        self.assertEqual(result["excluded_split_counts"], {"test": 55})
        self.assertFalse(result["training_approved"])
        selected = self.catalog["tasks"][:195]
        expected = {r["path"] for r in selected} | {result["catalog_path"], "datasets/analog_benchmark_250_v1/training_bundle_manifest.json"}
        actual = {str(p.relative_to(self.output)) for p in self.output.rglob("*") if p.is_file()}
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 197)
        for row in selected:
            self.assertEqual((self.output / row["path"]).read_bytes(), (self.root / row["path"]).read_bytes())
        for entry in result["files"]:
            self.assertEqual(sha(self.output / entry["path"]), entry["sha256"])
        for path, digest in result["source_inputs_sha256"].items():
            self.assertEqual(sha(self.root / path), digest)
        staged_catalog = self.output / result["catalog_path"]
        with patch("training.catalog.ROOT", self.output):
            loaded, records = load_catalog(staged_catalog, training=False)
            self.assertEqual(set(records), {row["id"] for row in selected})
            self.assertEqual(loaded["family_splits"], {"family_train": "train", "family_validation": "validation"})
            with patch("training.catalog.require_training_approval", side_effect=RuntimeError("not approved")) as gate:
                with self.assertRaisesRegex(RuntimeError, "not approved"):
                    load_catalog(staged_catalog, training=True)
                gate.assert_called_once()
        self.assertEqual(before, {p: sha(p) for p in self.bundle.rglob("*.json")})
        metadata = json.dumps(result) + staged_catalog.read_text()
        self.assertNotIn("reference_path", metadata)
        self.assertNotIn("task_249", metadata)
        self.assertFalse((self.bundle / "private").exists())

    def test_task_tampering_rejected_without_creating_output(self):
        path = self.bundle / self.rows[0]["path"]
        path.write_text(path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "Task changed"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_catalog_cannot_change_release_splits_or_source_binding(self):
        original = deepcopy(self.catalog)
        for kind in ("hash", "split", "family"):
            self.catalog = deepcopy(original)
            if kind == "hash":
                self.catalog["source_index_sha256"] = "0" * 64
            elif kind == "split":
                for row in self.catalog["tasks"][:174]:
                    row["split"] = "validation"
            else:
                self.catalog["tasks"][0]["topology_family"] = "wrong_family"
            self.catalog_path.write_text(json.dumps(self.catalog))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse(self.output.exists())

    def test_hash_bound_answer_fields_are_not_copied(self):
        path = self.bundle / self.rows[0]["path"]
        task = json.loads(path.read_text())
        task["solution_parameters"] = task["initial_parameters"]
        path.write_text(json.dumps(task))
        self.rows[0]["sha256"] = self.catalog["tasks"][0]["sha256"] = sha(path)
        self.write_sources()
        with self.assertRaisesRegex(ValueError, "Answer-bearing field"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_path_escape_and_duplicate_release_ids_are_rejected(self):
        original = deepcopy(self.index)
        for change in ({"path": "../private/answer.json"}, {"id": "task_001"}):
            self.index = deepcopy(original)
            self.index["tasks"][0].update(change)
            self.write_sources()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse(self.output.exists())

    def test_wrong_counts_and_family_split_leakage_are_rejected(self):
        original = deepcopy(self.index)
        self.index["tasks"][0]["split"] = "test"
        self.write_sources()
        with self.assertRaisesRegex(ValueError, "split counts"):
            self.prepare()
        self.index = original
        for row in self.index["tasks"][174:195]:
            row["topology_family"] = "family_train"
        for row in self.catalog["tasks"][174:195]:
            row["topology_family"] = "family_train"
        self.write_sources()
        with self.assertRaisesRegex(ValueError, "family occurs in different splits"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_existing_output_and_source_release_are_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("unchanged")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(sentinel.read_text(), "unchanged")
        with self.assertRaisesRegex(ValueError, "immutable source"):
            prepare_training_bundle(self.index_path, self.catalog_path, self.bundle / "new_output")
        self.assertFalse((self.bundle / "new_output").exists())


if __name__ == "__main__":
    unittest.main()
