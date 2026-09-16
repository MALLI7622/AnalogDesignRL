"""Deterministic design-of-experiments for verified benchmark source domains."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import math
from pathlib import Path
import random

from analog_design.simulator import ROOT
from benchmark.research.discover_designs import latin_hypercube
from benchmark.simulation import MeasurementCache, canonical_hash, save_json


def rounded_parameters(task, parameters):
    return {name: min(rule["max"], max(rule["min"], int(round(parameters[name])) if rule.get("integer")
                                        else float(f"{parameters[name]:.4g}")))
            for name, rule in task["parameters"].items()}


def neighboring_designs(task, seeds, count, seed):
    rng = random.Random(seed)
    samples = []
    for index in range(count):
        center = seeds[index % len(seeds)]["parameters"]
        radius = rng.uniform(0.025, 0.13)
        parameters = {}
        for name, rule in task["parameters"].items():
            coordinate = math.log(center[name] / rule["min"]) / math.log(rule["max"] / rule["min"])
            coordinate = min(1, max(0, coordinate + rng.gauss(0, radius)))
            parameters[name] = rule["min"] * (rule["max"] / rule["min"]) ** coordinate
        samples.append(rounded_parameters(task, parameters))
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "benchmark/domains.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=ROOT / "runs/benchmark_measurements_v1")
    parser.add_argument("--names", nargs="*")
    parser.add_argument("--count", type=int, default=192)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--neighbors", type=Path, help="Directory of prior per-topology design JSON arrays")
    args = parser.parse_args()
    if args.count < 1 or not 1 <= args.workers <= 8:
        parser.error("Positive count and 1–8 workers required.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    entries = json.loads(args.catalog.read_text())["entries"]
    entries = [entry for entry in entries if not args.names or entry["name"] in args.names]
    cache = MeasurementCache(args.cache)
    jobs = []
    summaries, progress = {}, {}
    for entry in entries:
        name, task = entry["name"], entry["task"]
        local_seed = args.seed + int(canonical_hash(name)[:8], 16)
        if args.neighbors:
            seeds = json.loads((args.neighbors / (name + ".json")).read_text())
            seeds = [row for row in seeds if row["status"] == "ok" and row["metrics"].get("phase_margin_deg", 0) >= 60]
            if not seeds:
                print(json.dumps({"topology": name, "status": "no_stable_seeds"}), flush=True)
                continue
            samples = neighboring_designs(task, seeds, args.count, local_seed)
        else:
            samples = [entry["public_default"], *latin_hypercube(task, args.count, local_seed)]
        seen = set()
        for index, parameters in enumerate(samples):
            parameters = rounded_parameters(task, parameters)
            fingerprint = canonical_hash(parameters)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            jobs.append((name, index, task, parameters))
        summaries[name] = []
        progress[name] = {"planned": len(seen), "completed": 0, "valid": 0, "stable_60deg": 0}
    random.Random(args.seed).shuffle(jobs)
    save_json(output / "plan.json", {"generation_mode": "source_grounded_deterministic_design_of_experiments",
                                     "seed": args.seed, "catalog": str(args.catalog.resolve()),
                                     "neighbors": str(args.neighbors) if args.neighbors else None,
                                     "progress": progress})
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(cache.measure, task, parameters): (name, index)
                   for name, index, task, parameters in jobs}
        for future in as_completed(pending):
            name, index = pending[future]
            result = future.result()
            record = {"sample_index": index, **{key: result[key] for key in
                      ("parameters", "status", "metrics", "measurement_key", "measurement_directory")},
                      "error": result.get("error")}
            summaries[name].append(record)
            total = progress[name]
            total["completed"] += 1
            total["valid"] += result["status"] == "ok"
            total["stable_60deg"] += result["status"] == "ok" and result["metrics"].get("phase_margin_deg", 0) >= 60
            save_json(output / "designs" / (name + ".json"), sorted(summaries[name], key=lambda row: row["sample_index"]))
            if sum(item["completed"] for item in progress.values()) % 25 == 0:
                print(json.dumps(progress), flush=True)
                save_json(output / "progress.json", progress)
    cache.assert_unchanged()
    save_json(output / "progress.json", progress)
    print(json.dumps(progress), flush=True)


if __name__ == "__main__":
    main()
