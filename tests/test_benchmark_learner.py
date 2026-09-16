from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.simulator import ROOT
from benchmark.learner import BenchmarkEpisode, export_learner_bundle, public_specification


def task():
    definition = json.loads((ROOT / "tasks/autockt_two_stage_sizing.json").read_text())
    definition["design_context"] = {
        "topology": "autockt_two_stage", "topology_family": "two_stage_miller",
        "circuit_description": "Two-stage amplifier with Miller compensation.",
        "paper": {"title": "AutoCkt", "authors": ["Fixture author"], "year": 2020,
                  "source_url": "https://arxiv.org/abs/2001.01808", "figure": "6", "review_notes": "internal review"},
        "netlist": (ROOT / "circuits/autockt_two_stage/netlist.spice").read_text(),
        "fixed_parameters": {"L_DEVICE": 1, "W_IN": 10},
        "scope": "Project adaptation with project-selected sizing targets.",
        "unlisted_note": "internal annotation"}
    return definition


class BenchmarkLearnerTests(unittest.TestCase):
    def test_public_specification_is_whitelisted_and_defensively_copied(self):
        original = task()
        original["internal_annotation"] = "private metadata that is not exported"
        result = public_specification(original)
        self.assertEqual(set(result), {"id", "purpose", "conditions", "parameters", "constraints", "ac", "transient",
                                       "max_evaluations", "current_parameters", "evaluations_remaining", "design_context"})
        self.assertNotIn("circuit_directory", result)
        self.assertNotIn("subcircuit", result)
        self.assertNotIn("initial_parameters", result)
        self.assertNotIn("unlisted_note", result["design_context"])
        self.assertNotIn("review_notes", result["design_context"]["paper"])
        self.assertEqual(result["current_parameters"], original["initial_parameters"])
        result["current_parameters"]["CCOMP"] = 0
        result["design_context"]["paper"]["authors"].append("changed")
        self.assertNotEqual(result["current_parameters"], original["initial_parameters"])
        self.assertEqual(original["design_context"]["paper"]["authors"], ["Fixture author"])

    def test_answer_fields_are_rejected_at_top_level_and_in_context(self):
        for field in ("reference_parameters", "public_default", "solution", "trajectory"):
            for nested in (False, True):
                with self.subTest(field=field, nested=nested):
                    definition = task()
                    destination = definition["design_context"] if nested else definition
                    destination[field] = {"CCOMP": 20e-12}
                    with self.assertRaisesRegex(ValueError, "Answer-bearing field"):
                        public_specification(definition)

    def test_fixed_editable_values_and_nonsymbolic_netlists_are_rejected(self):
        definition = task()
        definition["design_context"]["fixed_parameters"]["ccomp"] = 20e-12
        with self.assertRaisesRegex(ValueError, "editable parameter"):
            public_specification(definition)
        for changed in ("\n.param CCOMP=20p\n", '\n.include "answer.params"\n'):
            definition = task()
            definition["design_context"]["netlist"] += changed
            with self.assertRaises(ValueError):
                public_specification(definition)
        definition = task()
        definition["design_context"]["netlist"] = definition["design_context"]["netlist"].replace("'CCOMP'", "20p")
        with self.assertRaisesRegex(ValueError, "missing from the symbolic"):
            public_specification(definition)

    def make_bundle(self, directory):
        root = Path(directory) / "source"
        path = root / "tasks" / (task()["id"] + ".json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(task(), indent=2) + "\n")
        index = {"tasks": [{"id": task()["id"], "path": "tasks/" + path.name,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "topology": "autockt_two_stage", "topology_family": "two_stage_miller",
                            "difficulty": "easy", "split": "train",
                            "reference_path": "private/does_not_exist/reference.json",
                            "reference_sha256": "unused", "trajectory_path": "private/missing.json"}]}
        (root / "index.json").write_text(json.dumps(index))
        return root / "index.json", path

    def test_bundle_contains_only_specs_index_readme_and_never_reads_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            index, _ = self.make_bundle(directory)
            output = Path(directory) / "learner"
            manifest = export_learner_bundle(index, output)
            files = {str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()}
            self.assertEqual(files, {"index.json", "README.md", "tasks/" + task()["id"] + ".json"})
            entry = manifest["tasks"][0]
            self.assertEqual(set(entry), {"id", "path", "sha256", "topology", "family", "difficulty", "split"})
            self.assertEqual(entry["sha256"], hashlib.sha256((output / entry["path"]).read_bytes()).hexdigest())
            exported = json.loads((output / entry["path"]).read_text())
            self.assertEqual(exported, public_specification(task()))
            self.assertNotIn("reference_path", (output / "index.json").read_text())
            self.assertNotIn("reference.params", (output / entry["path"]).read_text())
            with self.assertRaises(FileExistsError):
                export_learner_bundle(index, output)

    def test_bundle_hash_changes_fail_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            index, source = self.make_bundle(directory)
            source.write_text(source.read_text() + " ")
            output = Path(directory) / "learner"
            with self.assertRaisesRegex(ValueError, "Task content changed"):
                export_learner_bundle(index, output)
            self.assertFalse(output.exists())

    def test_episode_exposes_context_and_keeps_live_values_and_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            path.write_text(json.dumps(task()))
            episode = BenchmarkEpisode(path, Path(directory) / "episode")

            def evaluate(path, parameters, run_dir, timeout):
                return {"task_id": task()["id"], "status": "ok", "success": False,
                        "reward": -0.1, "metrics": {}, "checks": {}, "parameters": parameters,
                        "simulator_invocations": 2, "elapsed_s": 0.01}

            with patch("analog_design.episode.evaluate", side_effect=evaluate):
                episode.step({"CCOMP": 1e-11})
            specification = episode.specification()
            self.assertEqual(specification["current_parameters"]["CCOMP"], 1e-11)
            self.assertEqual(specification["evaluations_remaining"], 29)
            self.assertEqual(specification["purpose"], task()["purpose"])
            self.assertEqual(specification["design_context"]["topology"], "autockt_two_stage")
            self.assertNotIn("circuit_directory", specification)
            specification["parameters"]["CCOMP"]["min"] = -1
            self.assertGreater(episode.specification()["parameters"]["CCOMP"]["min"], 0)

    def test_training_gate_is_inherited_and_never_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            path.write_text(json.dumps(task()))
            output = Path(directory) / "episode"
            with patch("analog_design.episode.require_training_approval", side_effect=RuntimeError("Training is not approved")) as gate:
                with self.assertRaisesRegex(RuntimeError, "Training is not approved"):
                    BenchmarkEpisode(path, output, for_training=True)
            gate.assert_called_once_with(path)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
