import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from analog_design.simulator import ROOT
from training.catalog import group_key, load_catalog
from training.client import RemoteEpisode, WorkerClient, WorkerError, action_prompt, scalar_task_id
from training.train import batches
from training.worker import EpisodeService, ServiceError, WorkerServer

CATALOG = ROOT / "training/configs/smoke_tasks.json"
TASK_ID = "autockt_two_stage_sizing_v1"


class LocalClient:
    def __init__(self, service):
        self.service = service

    def request(self, path, payload=None):
        if path == "/episodes":
            return self.service.create(payload["task_id"])
        if path == "/step":
            return self.service.step(payload["episode_id"], payload["step"], payload["action"])
        if path == "/close":
            return self.service.close(payload["episode_id"])
        raise AssertionError(path)


def evaluator(scores):
    def evaluate(task_path, action, output, timeout_s):
        score = next(scores)
        return {"task_id": TASK_ID, "status": "ok", "success": score == 1.0, "reward": score,
                "metrics": {"gain_db": 55.0}, "checks": {"gain_db": score == 1.0},
                "elapsed_s": 0.1, "simulator_invocations": 2}
    return evaluate


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = EpisodeService(CATALOG, self.directory.name)

    def test_training_rejects_smoke_catalog(self):
        with self.assertRaises(ValueError):
            EpisodeService(CATALOG, self.directory.name, mode="training")

    def test_batched_task_id_is_normalized_before_http_serialization(self):
        from types import SimpleNamespace
        for task_id in (TASK_ID, [TASK_ID], [[TASK_ID]], SimpleNamespace(tolist=lambda: [TASK_ID])):
            remote = RemoteEpisode(LocalClient(self.service), task_id, max_steps=1, required_mode="evaluation")
            self.assertEqual(remote.task_id, TASK_ID)
            remote.reset()
            remote.close()
        for value in ([], ['a', 'b'], 42, '', None):
            with self.assertRaises(ValueError):
                scalar_task_id(value)

    def test_training_requires_existing_qualification(self):
        manifest = json.loads(CATALOG.read_text())
        for task in manifest["tasks"]:
            task["split"] = "train"
        path = Path(self.directory.name) / "catalog.json"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "not approved"):
            load_catalog(path, training=True)

    def test_catalog_detects_hash_change_and_group_leakage(self):
        manifest = json.loads(CATALOG.read_text())
        path = Path(self.directory.name) / "catalog.json"
        manifest["tasks"][0]["sha256"] = "wrong"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "changed"):
            load_catalog(path)
        # Different task IDs and initial sizes still belong to the same topology.
        manifest = json.loads(CATALOG.read_text())
        nominal = json.loads((ROOT / "tasks/fan_smc_nominal.json").read_text())
        from analog_design.simulator import digest
        manifest["tasks"][0]["split"] = "train"
        manifest["tasks"].append({**manifest["tasks"][0], "id": nominal["id"],
                                  "path": "tasks/fan_smc_nominal.json", "split": "test",
                                  "sha256": digest(ROOT / "tasks/fan_smc_nominal.json")})
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "different splits"):
            load_catalog(path)

    def test_requirement_group_ignores_numeric_formatting_and_starting_sizes(self):
        import copy
        task = json.loads((ROOT / "tasks/autockt_two_stage_sizing.json").read_text())
        variant = copy.deepcopy(task)
        variant["constraints"]["gain_db"]["min"] = 50.0
        variant["initial_parameters"]["CCOMP"] = 1e-11
        self.assertEqual(group_key(task, "requirements"), group_key(variant, "requirements"))
        variant["constraints"]["gain_db"]["min"] = 60.0
        self.assertNotEqual(group_key(task, "requirements"), group_key(variant, "requirements"))

    def test_retry_is_idempotent_but_changed_action_is_rejected(self):
        key = self.service.create(TASK_ID)["episode_id"]
        with patch("analog_design.episode.evaluate", side_effect=evaluator(iter([-0.5]))) as mock:
            first = self.service.step(key, 1, {})
            self.assertEqual(first, self.service.step(key, 1, {}))
            self.assertEqual(mock.call_count, 1)
            with self.assertRaises(ServiceError) as caught:
                self.service.step(key, 1, {"IBIAS": 3e-5})
            self.assertEqual(caught.exception.status, 409)
            with self.assertRaises(ServiceError):
                self.service.step(key, 3, {})

    def test_concurrent_retries_run_simulator_once(self):
        from concurrent.futures import ThreadPoolExecutor
        key = self.service.create(TASK_ID)["episode_id"]
        with patch("analog_design.episode.evaluate", side_effect=evaluator(iter([-0.5]))) as mock:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.service.step(key, 1, {}), range(4)))
            self.assertEqual(mock.call_count, 1)
            self.assertTrue(all(result == results[0] for result in results))

    def test_episode_total_is_final_score_and_budget_cap_is_visible(self):
        remote = RemoteEpisode(LocalClient(self.service), TASK_ID, max_steps=3, required_mode="evaluation")
        self.assertEqual(json.loads(remote.reset())["evaluations_remaining"], 3)
        with patch("analog_design.episode.evaluate", side_effect=evaluator(iter([-0.5, -0.2, 1.0]))):
            transitions = [remote.step("{}") for _ in range(3)]
        self.assertAlmostEqual(sum(step[1] for step in transitions), 1.0)
        self.assertEqual([step[2] for step in transitions], [False, False, True])
        self.assertEqual(json.loads(transitions[0][0])["failed_requirements"], ["gain_db"])
        remote.close()
        self.assertEqual(len(self.service.sessions), 0)

    def test_invalid_action_spends_budget_and_gets_negative_reward(self):
        remote = RemoteEpisode(LocalClient(self.service), TASK_ID, max_steps=1, required_mode="evaluation")
        remote.reset()
        # Real evaluator rejects the malformed action before launching ngspice.
        feedback, reward, done, info = remote.step('{"IBIAS": 1e-5, "IBIAS": 2e-5}')
        self.assertEqual(reward, -1.0)
        self.assertTrue(done)
        self.assertEqual(info["simulator_invocations"], 0)
        self.assertEqual(json.loads(feedback)["evaluations_remaining"], 0)
        with self.assertRaises(WorkerError):
            remote.step("{}")
        remote.close()

    def test_infrastructure_exception_does_not_become_model_reward(self):
        remote = RemoteEpisode(LocalClient(self.service), TASK_ID, max_steps=2, required_mode="evaluation")
        remote.reset()
        with patch("analog_design.episode.evaluate", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                remote.step("{}")
            with self.assertRaises(ServiceError):
                remote.step("{}")
        self.assertEqual(remote.steps, 0)
        self.assertEqual(remote.previous_score, 0)

    def test_markdown_rejected_with_format_feedback_then_raw_json_reaches_evaluator(self):
        remote = RemoteEpisode(LocalClient(self.service), TASK_ID, max_steps=2, required_mode="evaluation")
        initial = json.loads(remote.reset())
        prompt = action_prompt(json.dumps(initial))
        self.assertIn(json.dumps(initial["current_parameters"], separators=(",", ":")), prompt)
        self.assertIn("without Markdown fences", prompt)
        feedback, reward, done, info = remote.step('```json\n{}\n```')
        self.assertEqual(reward, -1.0)
        self.assertFalse(done)
        self.assertEqual(info["simulator_invocations"], 0)
        self.assertEqual(json.loads(feedback)["evaluations_remaining"], 1)
        self.assertIn("Remove Markdown", json.loads(feedback)["error"])
        with patch("analog_design.episode.evaluate", side_effect=evaluator(iter([-0.25]))) as mock:
            feedback, reward, done, info = remote.step('{}')
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(info["verifier_reward"], -0.25)
        self.assertAlmostEqual(reward, 0.75)
        self.assertTrue(done)
        self.assertNotIn("error", json.loads(feedback))
        remote.close()

    def test_trajectory_keeps_raw_response_feedback_and_episode_identity(self):
        directory = Path(self.directory.name) / "trajectories"
        remote = RemoteEpisode(LocalClient(self.service), TASK_ID, max_steps=1,
                               required_mode="evaluation", trajectory_directory=directory)
        remote.reset()
        episode_id = remote.key
        response = '```json\n{}\n```'
        remote.step(response)
        remote.close()
        paths = list(directory.glob('*.jsonl'))
        self.assertEqual(len(paths), 1)
        rows = [json.loads(line) for line in paths[0].read_text().splitlines()]
        self.assertEqual([r['event'] for r in rows], ['initial', 'action', 'feedback', 'closed'])
        self.assertEqual(rows[1]['response'], response)
        self.assertEqual(rows[2]['observation']['reward'], -1)
        self.assertEqual(rows[2]['reward_delta'], -1)
        self.assertTrue(all(r['episode_id'] == episode_id for r in rows))
        self.assertNotIn('reference', json.dumps(rows))
        remote.reset()
        remote.close()
        self.assertEqual(len(list(directory.glob('*.jsonl'))), 2)
        remote.close()

    def test_capacity_and_mode_mismatch_release(self):
        self.service.max_active = 1
        remote = RemoteEpisode(LocalClient(self.service), TASK_ID, max_steps=1, required_mode="training")
        with self.assertRaises(WorkerError):
            remote.reset()
        self.assertEqual(len(self.service.sessions), 0)
        key = self.service.create(TASK_ID)["episode_id"]
        with self.assertRaises(ServiceError):
            self.service.create(TASK_ID)
        self.service.close(key)
        self.service.create(TASK_ID)

    def test_http_auth_and_catalog_hide_private_paths(self):
        token = "test-token-with-at-least-24-characters"
        server = WorkerServer(("127.0.0.1", 0), self.service, token, workers=2)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(WorkerError):
                WorkerClient(url, "wrong-token-with-24-characters").request("/catalog")
            client = WorkerClient(url, token)
            catalog = client.request("/catalog")
            self.assertNotIn("task_path", json.dumps(catalog))
            self.assertNotIn("reference", json.dumps(catalog))
            episode = client.request("/episodes", {"task_id": TASK_ID})
            self.assertNotIn("circuit_directory", episode["specification"])
            client.request("/close", {"episode_id": episode["episode_id"]})
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_batches_are_reproducible_and_contain_no_answers(self):
        tasks = [{"id": "a", "reference": "private"}, {"id": "b"}]
        result = list(batches(tasks, 10, 42))
        self.assertEqual(result, list(batches(tasks, 10, 42)))
        self.assertNotIn("private", json.dumps(result))
        self.assertTrue(all(set(batch) == {"prompts", "task_id"} for batch in result))


if __name__ == "__main__":
    unittest.main()
