from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analog_design.simulator import ROOT, digest
from training.catalog import group_key
from training.research_pilot import prepare_manifest, validate_trainer_manifest
from training.worker import EpisodeService, ServiceError


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


class ResearchPilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="research_pilot_test_", dir=ROOT / "runs")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        entries = []
        smoke = json.loads((ROOT / "training/configs/smoke_tasks.json").read_text())
        for number, row in enumerate(smoke["tasks"][:2]):
            task = json.loads((ROOT / row["path"]).read_text())
            path = self.directory / f"task_{number}.json"
            write(path, task)
            entries.append({"id": task["id"], "path": str(path.relative_to(ROOT)), "sha256": digest(path),
                            "group": group_key(task, "topology"), "split": "train" if number == 0 else "validation"})
        self.catalog = self.directory / "catalog.json"
        write(self.catalog, {"schema_version": 1, "split_policy": "topology", "tasks": entries})
        self.task_id = entries[0]["id"]
        self.config = {"max_updates": 1, "num_generations": 2, "max_episode_steps": 2}
        self.config_path = self.directory / "config.json"
        write(self.config_path, self.config)
        self.evidence = self.directory / "evidence.json"
        write(self.evidence, {"scope": "Synthetic unit-test evidence; no real authorization"})
        self.manifest_path = self.directory / "pilot.json"
        self.draft = prepare_manifest(self.catalog, self.config_path, [self.evidence],
                                      max_episodes=3, max_evaluations_per_episode=2, max_total_evaluations=6)
        write(self.manifest_path, self.draft)

    def authorize_fixture(self, changes=None):
        record = deepcopy(self.draft)
        record["authorized"] = True
        record["authorization"] = {"authorized_by": "synthetic unit-test fixture",
                                   "authorized_utc": datetime.now(timezone.utc).isoformat(),
                                   "basis": "Unit test only; this is not user authorization for a real run"}
        if changes:
            changes(record)
        write(self.manifest_path, record)
        usage = ROOT / "runs/research_pilot_usage" / (digest(self.manifest_path) + ".json")
        self.addCleanup(usage.unlink, missing_ok=True)
        return record

    def service(self):
        return EpisodeService(self.catalog, self.directory / "worker", mode="research-pilot",
                              pilot_manifest=self.manifest_path)

    def evaluator(self, task_path, action, output, timeout_s):
        task = json.loads(Path(task_path).read_text())
        return {"task_id": task["id"], "status": "ok", "success": False, "reward": -0.5,
                "metrics": {"gain_db": 55.0}, "checks": {"gain_db": False},
                "elapsed_s": 0.1, "simulator_invocations": 2}

    def test_draft_cannot_start_worker_or_claim_usage(self):
        self.assertFalse(self.draft["authorized"])
        self.assertFalse(self.draft["qualified_training_approved"])
        with self.assertRaisesRegex(ValueError, "not explicitly authorized"):
            self.service()
        self.assertFalse((self.directory / "worker").exists())

    def test_expiry_and_missing_authorization_identity_rejected(self):
        self.authorize_fixture(lambda record: record.update(expires_utc=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
        with self.assertRaisesRegex(ValueError, "expired"):
            self.service()
        self.authorize_fixture(lambda record: record.update(authorization={}))
        with self.assertRaisesRegex(ValueError, "who, when"):
            self.service()

    def test_catalog_cannot_include_test_or_smoke(self):
        record = json.loads(self.catalog.read_text())
        for forbidden in ("test", "smoke"):
            record["tasks"][1]["split"] = forbidden
            write(self.catalog, record)
            with self.assertRaisesRegex(ValueError, "test and smoke"):
                prepare_manifest(self.catalog, self.config_path, [self.evidence])

    def test_normal_training_gate_is_unchanged_and_manifest_cannot_be_smuggled(self):
        self.authorize_fixture()
        with self.assertRaisesRegex(RuntimeError, "not approved"):
            EpisodeService(self.catalog, self.directory / "normal", mode="training")
        for mode in ("training", "evaluation"):
            with self.assertRaisesRegex(ValueError, "Only research-pilot"):
                EpisodeService(self.catalog, self.directory / mode, mode=mode, pilot_manifest=self.manifest_path)
        with self.assertRaisesRegex(ValueError, "Only research-pilot"):
            EpisodeService(self.catalog, self.directory / "missing", mode="research-pilot")

    def test_complete_bindings_and_current_config_are_required(self):
        self.authorize_fixture(lambda record: record["inputs_sha256"].pop("analog_design/metrics.py"))
        with self.assertRaisesRegex(ValueError, "omits required"):
            self.service()
        self.authorize_fixture()
        write(self.config_path, {**self.config, "max_updates": 2})
        with self.assertRaisesRegex(ValueError, "changed|exceeds"):
            self.service()

    def test_task_hash_changes_rejected(self):
        self.authorize_fixture()
        task_path = self.directory / "task_0.json"
        task = json.loads(task_path.read_text())
        task["purpose"] = "changed unit-test task"
        write(task_path, task)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.service()

    def test_server_caps_attempts_retries_and_closed_episodes(self):
        self.authorize_fixture()
        service = self.service()
        first = service.create(self.task_id)
        self.assertEqual(first["specification"]["evaluations_remaining"], 2)
        with patch("analog_design.episode.evaluate", side_effect=self.evaluator) as evaluate:
            observed = service.step(first["episode_id"], 1, {})
            self.assertEqual(service.step(first["episode_id"], 1, {}), observed)
            final = service.step(first["episode_id"], 2, {})
            self.assertTrue(final["truncated"])
            self.assertEqual(final["evaluations_remaining"], 0)
            with self.assertRaises(ServiceError):
                service.step(first["episode_id"], 3, {})
            self.assertEqual(evaluate.call_count, 2)
        service.close(first["episode_id"])
        for _ in range(2):
            service.close(service.create(self.task_id)["episode_id"])
        with self.assertRaisesRegex(ServiceError, "episode limit exhausted"):
            service.create(self.task_id)
        usage = json.loads(service.pilot.usage_path.read_text())
        self.assertEqual((usage["episodes_reserved"], usage["evaluations_reserved"]), (3, 2))

    def test_total_attempt_cap_cannot_be_bypassed_with_new_episodes(self):
        self.authorize_fixture(lambda record: record["limits"].update(max_total_evaluations=4))
        service = self.service()
        with patch("analog_design.episode.evaluate", side_effect=self.evaluator) as evaluate:
            for _ in range(2):
                key = service.create(self.task_id)["episode_id"]
                service.step(key, 1, {})
                service.step(key, 2, {})
                service.close(key)
            key = service.create(self.task_id)["episode_id"]
            with self.assertRaisesRegex(ServiceError, "evaluation limit exhausted"):
                service.step(key, 1, {})
            self.assertEqual(evaluate.call_count, 4)

    def test_failed_physical_attempt_is_reserved_and_restart_cannot_reset_caps(self):
        self.authorize_fixture()
        service = self.service()
        key = service.create(self.task_id)["episode_id"]
        with patch("analog_design.episode.evaluate", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                service.step(key, 1, {})
        self.assertEqual(json.loads(service.pilot.usage_path.read_text())["evaluations_reserved"], 1)
        with self.assertRaisesRegex(ValueError, "already used"):
            EpisodeService(self.catalog, self.directory / "other", mode="research-pilot", pilot_manifest=self.manifest_path)

    def test_manifest_mutation_while_alive_blocks_next_attempt(self):
        self.authorize_fixture()
        service = self.service()
        key = service.create(self.task_id)["episode_id"]
        record = json.loads(self.manifest_path.read_text())
        record["limits"]["max_total_evaluations"] = 5
        write(self.manifest_path, record)
        with patch("analog_design.episode.evaluate") as evaluate:
            with self.assertRaisesRegex(ServiceError, "changed while running"):
                service.step(key, 1, {})
            evaluate.assert_not_called()

    def test_expiry_while_alive_blocks_work_and_concurrent_creates_obey_cap(self):
        from concurrent.futures import ThreadPoolExecutor
        self.authorize_fixture()
        service = self.service()
        def create(_):
            try:
                return service.create(self.task_id)["episode_id"]
            except ServiceError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            created = [key for key in pool.map(create, range(8)) if key]
        self.assertEqual(len(created), 3)
        self.assertEqual(json.loads(service.pilot.usage_path.read_text())["episodes_reserved"], 3)
        with patch("training.research_pilot._now", return_value=datetime.now(timezone.utc) + timedelta(days=1)), \
                patch("analog_design.episode.evaluate") as evaluate:
            with self.assertRaisesRegex(ServiceError, "expired"):
                service.step(created[0], 1, {})
            evaluate.assert_not_called()

    def test_conflicting_evidence_cannot_override_an_input_binding(self):
        self.authorize_fixture(lambda record: record["evidence_sha256"].update({"analog_design/metrics.py": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "Conflicting pilot"):
            self.service()

    def test_trainer_binds_exact_worker_config_and_qualification_scope(self):
        self.authorize_fixture()
        service = self.service()
        catalog = service.catalog()
        binding = validate_trainer_manifest(self.manifest_path, self.config, catalog)
        self.assertTrue(binding["research_pilot"])
        self.assertFalse(binding["qualified_training_approved"])
        self.assertEqual(binding["independent_expert_review"], "pending")
        self.assertNotIn("reference.params", json.dumps(catalog))
        for bad in ({**catalog, "mode": "evaluation"}, {**catalog, "pilot_manifest_sha256": "wrong"},
                    {**catalog, "catalog_sha256": "wrong"}, {**catalog, "qualified_training_approved": True}):
            with self.assertRaises(ValueError):
                validate_trainer_manifest(self.manifest_path, self.config, bad)
        with self.assertRaisesRegex(ValueError, "Trainer config differs"):
            validate_trainer_manifest(self.manifest_path, {**self.config, "max_updates": 2}, catalog)


if __name__ == "__main__":
    unittest.main()
