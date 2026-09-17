"""Pure tests for pilot selection and checkpoint provenance, without JAX or TPU."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from training.train import (argument_parser, closing_cluster, execution_plan, restore_adapter,
                            restore_source, save_final_checkpoint, select_tasks,
                            validate_config, validate_restored_checkpoint, validate_run_arguments)

ROOT = Path(__file__).resolve().parents[1]


class RunControlTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "training/configs/gemma3_1b.json").read_text())

    def test_limit_preserves_frozen_catalog_and_is_order_independent(self):
        catalog = {"tasks": [{"id": "val-z", "split": "validation"},
                             {"id": "test-a", "split": "test"},
                             {"id": "val-a", "split": "validation"},
                             {"id": "train-a", "split": "train"}]}
        original = copy.deepcopy(catalog)
        self.assertEqual([r["id"] for r in select_tasks(catalog, "validation", 1)], ["val-a"])
        self.assertEqual(catalog, original)
        reversed_catalog = {"tasks": list(reversed(catalog["tasks"]))}
        self.assertEqual(select_tasks(catalog, "validation"), select_tasks(reversed_catalog, "validation"))
        self.assertEqual(len(select_tasks(catalog, "validation", 10)), 2)
        with self.assertRaisesRegex(ValueError, "No tasks"):
            select_tasks(catalog, "smoke", 1)
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                select_tasks(catalog, "train", limit)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            select_tasks({"tasks": catalog["tasks"] * 2}, "train")

    def test_two_update_plan_counts_step_zero_validation(self):
        config = {**self.config, "max_updates": 2, "validation_task_limit": 3, "eval_every": 5}
        validate_config(config)
        plan = execution_plan(config, validation_count=3)
        self.assertEqual(plan["validation_rounds"], 1)
        self.assertEqual(plan["max_training_episodes"], 8)
        self.assertEqual(plan["max_validation_episodes"], 12)
        self.assertEqual(plan["max_total_episodes"], 20)
        self.assertEqual(plan["max_total_evaluations"], 80)
        self.assertEqual(execution_plan(config, validation_count=1)["max_total_evaluations"], 48)
        uncapped = execution_plan(self.config)
        self.assertEqual(uncapped["validation_rounds"], 4)
        self.assertEqual(uncapped["max_training_simulator_invocations"], 640)
        self.assertIsNone(uncapped["max_total_evaluations"])
        for limit in (None, 0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "validation_task_limit"):
                validate_config({**config, "validation_task_limit": limit})

    def test_cli_controls_are_explicit_and_do_not_enable_training_resume(self):
        parser = argument_parser()
        for flags in (["--mode", "rollout", "--max-tasks", "1"],
                      ["--mode", "rollout", "--restore-checkpoint", "saved", "--restore-run", "run.json"],
                      ["--mode", "research-pilot", "--pilot-manifest", "manifest.json"], []):
            validate_run_arguments(parser.parse_args(flags))
        validate_run_arguments(parser.parse_args(['--mode', 'train', '--restore-checkpoint', 'saved',
                                                  '--restore-run', 'run.json', '--warm-start-adapter']))
        for flags in (['--warm-start-adapter'], ['--mode', 'train', '--warm-start-adapter'],
                      ['--mode', 'rollout', '--restore-checkpoint', 'saved', '--restore-run', 'run.json', '--warm-start-adapter']):
            with self.assertRaises(ValueError): validate_run_arguments(parser.parse_args(flags))
        invalid = [
            ["--mode", "train", "--max-tasks", "1"],
            ["--mode", "rollout", "--max-tasks", "0"],
            ["--mode", "rollout", "--restore-checkpoint", "saved"],
            ["--mode", "rollout", "--restore-run", "run.json"],
            ["--mode", "train", "--restore-checkpoint", "saved", "--restore-run", "run.json"],
            ["--mode", "research-pilot"],
            ["--mode", "train", "--pilot-manifest", "manifest.json"],
        ]
        for flags in invalid:
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                validate_run_arguments(parser.parse_args(flags))

    def test_cluster_closes_once_for_upstream_close_and_exception(self):
        cluster = SimpleNamespace(close=Mock())
        actual_close = cluster.close
        finalize = Mock()
        with closing_cluster(cluster, finalize):
            cluster.close()  # Pinned learner closes internally on success.
            cluster.close()
        actual_close.assert_called_once_with()
        finalize.assert_called_once_with(cluster)
        cluster = SimpleNamespace(close=Mock())
        actual_close = cluster.close
        with self.assertRaisesRegex(RuntimeError, "rollout failed"):
            with closing_cluster(cluster):
                raise RuntimeError("rollout failed")
        actual_close.assert_called_once_with()

    def test_final_save_retains_binding_and_releases_resources_on_failure(self):
        manager = Mock()
        manager.latest_step.return_value = None
        manager.save.return_value = True
        metadata = {"global_step": 3, "role": "actor", "source_run_sha256": "binding", "checkpoint_step": 2}
        trainer = SimpleNamespace(train_steps=2, checkpoint_manager=manager, model=object(),
                                  optimizer=object(), custom_checkpoint_metadata=lambda: metadata)
        cluster = SimpleNamespace(actor_trainer=trainer, global_steps=2)
        save_final_checkpoint(cluster)
        kwargs = manager.save.call_args.kwargs
        self.assertTrue(kwargs["force"])
        self.assertTrue(kwargs["save_only_lora_params"])
        self.assertEqual(kwargs["custom_metadata"], {**metadata, "global_step": 2})
        manager.reset_mock()
        manager.latest_step.return_value = 2
        save_final_checkpoint(cluster)
        manager.save.assert_not_called()
        manager.latest_step.return_value = None
        manager.save.return_value = False
        cluster.close = Mock()
        actual_close = cluster.close
        with self.assertRaisesRegex(RuntimeError, "not saved"):
            with closing_cluster(cluster, save_final_checkpoint):
                pass
        actual_close.assert_called_once_with()


class RestoreBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = str(self.directory / "checkpoints")
        self.config = json.loads((ROOT / "training/configs/gemma3_1b.json").read_text())
        self.revision = "a" * 40
        self.run = {"mode": "research-pilot", "config": {**self.config, "model_revision": self.revision},
                    "checkpoint_uri": self.root, "qualified_training_approved": False}
        self.run_path = self.directory / "run.json"
        self.run_path.write_text(json.dumps(self.run))
        self.adapter_hash = "b" * 64
        self.result = {"optimizer_steps": 2, "checkpoint_step": 2, "checkpoint_root": self.root,
                       "final_adapter_sha256": self.adapter_hash}
        self.result_path = self.directory / "training_result.json"
        self.result_path.write_text(json.dumps(self.result))
        self.source = restore_source(self.root, self.run_path, self.config)
        self.metadata = {key: self.source[key] for key in
                         ("source_run_sha256", "model_id", "model_revision", "lora_config_sha256")}
        self.metadata.update(global_step=2, role="actor", checkpoint_step=2)

    def test_source_pins_base_revision_without_changing_requested_config(self):
        self.assertEqual(self.source["model_revision"], self.revision)
        self.assertEqual(self.config["model_revision"], "main")
        self.assertEqual(self.source["source_mode"], "research-pilot")
        self.assertEqual(self.source["source_run_sha256"], hashlib.sha256(self.run_path.read_bytes()).hexdigest())
        self.assertEqual(self.source["source_result"]["checkpoint_step"], 2)

    def test_rejects_different_base_lora_root_and_nontraining_source(self):
        for changed in ({**self.config, "model_id": "different"},
                        {**self.config, "lora": {"rank": 8}},
                        {**self.config, "model_revision": "c" * 40}):
            with self.subTest(config=changed), self.assertRaises(ValueError):
                restore_source(self.root, self.run_path, changed)
        with self.assertRaisesRegex(ValueError, "root"):
            restore_source(self.root + "-other", self.run_path, self.config)
        for changed in ({**self.run, "mode": "rollout"},
                        {**self.run, "config": {**self.run["config"], "model_revision": "main"}}):
            self.run_path.write_text(json.dumps(changed))
            with self.subTest(run=changed), self.assertRaises(ValueError):
                restore_source(self.root, self.run_path, self.config)

    def test_completed_source_requires_final_step_metadata_and_hash(self):
        verified = validate_restored_checkpoint(self.source, 2, self.metadata, self.adapter_hash)
        self.assertTrue(verified["final_adapter_hash_verified"])
        for step, metadata, digest in ((0, self.metadata, self.adapter_hash),
                                       (2, {}, self.adapter_hash),
                                       (2, {**self.metadata, "global_step": 0}, self.adapter_hash),
                                       (2, {**self.metadata, "model_revision": "c" * 40}, self.adapter_hash),
                                       (2, {**self.metadata, "source_run_sha256": "wrong"}, self.adapter_hash),
                                       (2, {**self.metadata, "checkpoint_step": 1}, self.adapter_hash),
                                       (1, {**self.metadata, "checkpoint_step": 1}, self.adapter_hash),
                                       (2, self.metadata, "d" * 64)):
            with self.subTest(step=step, metadata=metadata, digest=digest), self.assertRaises(RuntimeError):
                validate_restored_checkpoint(self.source, step, metadata, digest)

    def test_incomplete_source_is_explicit_and_still_requires_bound_checkpoint(self):
        self.result_path.unlink()
        source = restore_source(self.root, self.run_path, self.config)
        verified = validate_restored_checkpoint(source, 2, self.metadata, self.adapter_hash)
        self.assertFalse(verified["final_adapter_hash_verified"])
        with self.assertRaises(RuntimeError):
            validate_restored_checkpoint(source, 2, {}, self.adapter_hash)

    def test_restore_uses_latest_lora_only_and_always_closes_manager(self):
        manager = Mock()
        manager.latest_step.return_value = 2
        manager.maybe_restore.return_value = (2, self.metadata)
        factory = Mock(return_value=manager)
        model = object()
        result = restore_adapter(model, self.source, lambda _: self.adapter_hash, factory)
        self.assertTrue(result["final_adapter_hash_verified"])
        factory.assert_called_once_with(self.root + "/actor")
        manager.maybe_restore.assert_called_once_with(model, step=2, restore_only_lora_params=True)
        manager.close.assert_called_once_with()
        for latest, restored in ((None, (0, {})), (0, (0, {})), (2, (0, {})), (2, (2, {}))):
            manager = Mock()
            manager.latest_step.return_value = latest
            manager.maybe_restore.return_value = restored
            with self.subTest(latest=latest, restored=restored), self.assertRaises(RuntimeError):
                restore_adapter(model, self.source, lambda _: self.adapter_hash, lambda _: manager)
            manager.close.assert_called_once_with()

    def test_changed_source_is_rejected_before_opening_checkpoint(self):
        factory = Mock()
        self.run_path.write_text(json.dumps({**self.run, "changed": True}))
        with self.assertRaisesRegex(RuntimeError, "Source run changed"):
            restore_adapter(object(), self.source, lambda _: self.adapter_hash, factory)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
