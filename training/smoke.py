"""Exercise real CPU simulations over HTTP with public diagnostic fixtures; no LLM training."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import statistics
import threading
import time

from analog_design.simulator import ROOT
from training.client import RemoteEpisode, WorkerClient
from training.preflight import check_simulator
from training.worker import EpisodeService, WorkerServer


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", default=None)
    args = cli.parse_args()
    output = Path(args.output or f"runs/worker_smoke_{time.time_ns()}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    hardware = check_simulator()
    service = EpisodeService(ROOT / "training/configs/smoke_tasks.json", output / "episodes")
    token = secrets.token_urlsafe(32)
    server = WorkerServer(("127.0.0.1", 0), service, token, workers=2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    start = time.monotonic()

    def exercise(task):
        # Public installation fixtures are used only here, outside all learner inputs.
        fixture = ROOT / task["path"].replace("_sizing.json", "_nominal.json")
        passing = json.loads(fixture.read_text())["initial_parameters"]
        client = WorkerClient(f"http://127.0.0.1:{server.server_port}", token)
        episode = RemoteEpisode(client, task["id"], max_steps=3, required_mode="evaluation")
        try:
            specification = episode.reset()
            if "reference.params" in specification or "circuit_directory" in specification:
                raise AssertionError("Private evaluator fields leaked")
            first, delta1, done1, info1 = episode.step("{}")
            before = len(service.sessions[episode.key]["episode"].history)
            retry = client.request("/step", {"episode_id": episode.key, "step": 1, "action": {}})
            if len(service.sessions[episode.key]["episode"].history) != before or retry["reward"] != delta1:
                raise AssertionError("Retry spent an additional evaluation")
            _, delta2, done2, info2 = episode.step(json.dumps(passing))
            if done1 or not done2 or info2["verifier_reward"] != 1.0 or abs(delta1 + delta2 - 1.0) > 1e-9:
                raise AssertionError("Fail-to-pass reward or termination is incorrect")
            return {"task_id": task["id"], "initial_reward": info1["verifier_reward"], "final_reward": info2["verifier_reward"],
                    "episode_reward": delta1 + delta2, "retry_did_not_spend_budget": True,
                    "evaluation_seconds": [info1["elapsed_s"], info2["elapsed_s"]],
                    "simulator_invocations": info1["simulator_invocations"] + info2["simulator_invocations"]}
        finally:
            episode.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(exercise, service.manifest["tasks"]))
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    elapsed = time.monotonic() - start
    sizes = [p.stat().st_size for p in output.rglob("*") if p.is_file()]
    latencies = [seconds for item in results for seconds in item["evaluation_seconds"]]
    report = {"status": "passed", "scope": "CPU HTTP integration using known public solutions; no model training",
              "environment": hardware, "concurrent_episodes": 2, "wall_seconds": elapsed,
              "mean_evaluation_seconds": statistics.mean(latencies), "artifact_bytes": sum(sizes),
              "tasks": results, "tpu_runtime_tested": False}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"Report: {output / 'report.json'}")


if __name__ == "__main__":
    main()
