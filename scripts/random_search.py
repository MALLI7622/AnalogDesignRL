"""A seeded random-search baseline using the same evaluation budget as an agent."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import random
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analog_design.episode import Episode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, default=ROOT / "tasks/fan_smc_sizing.json")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "runs" / ("random_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    episode = Episode(args.task, output)
    rng = random.Random(args.seed)
    observation = episode.step({})  # The initial design consumes one evaluation.
    while not episode.done:
        action = {}
        for name, rule in episode.specification()["parameters"].items():
            if rule.get("integer"):
                action[name] = rng.randint(math.ceil(rule["min"]), math.floor(rule["max"]))
            else:
                action[name] = rng.uniform(rule["min"], rule["max"])
        observation = episode.step(action)
    summary = {"method": "uniform random search over linear parameter ranges",
               "seed": args.seed, "task_id": episode.task["id"],
               "success": observation["success"], "max_evaluations": episode.budget,
               "evaluations_used": len(episode.history),
               "simulator_invocations": sum(r["simulator_invocations"] for r in episode.history),
               "failed_evaluations": sum(r["status"] != "ok" for r in episode.history),
               "elapsed_s": sum(r["elapsed_s"] for r in episode.history),
               "final_parameters": observation["parameters"], "final_metrics": observation["metrics"]}
    (episode.directory / "search.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"run_directory": str(episode.directory), **summary}, indent=2))
    return 0 if observation["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
