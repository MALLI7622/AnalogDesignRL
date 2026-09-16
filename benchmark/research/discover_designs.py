"""Bounded deterministic design exploration on source-provided amplifier circuits."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import math
from pathlib import Path
import random
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmark.research.probe_amplifiers import parse_parameters
from benchmark.simulation import MeasurementCache, canonical_hash, save_json


def source_task(name, probe_directory):
    circuit = probe_directory / "circuits" / name
    task = json.loads((circuit / "probe.json").read_text())
    values = parse_parameters(circuit / "reference.params")
    names = [key for key in values if key.startswith(("CAPACITOR_", "CURRENT_")) or
             re.search(r"_M_gm[123]_", key)]
    task["parameters"] = {}
    task["initial_parameters"] = {}
    for key in sorted(names):
        value = values[key]
        integer = "_M_" in key
        rule = {"min": max(1, math.ceil(value / 4)) if integer else value / 4,
                "max": min(512, max(4, round(value * 4))) if integer else value * 4,
                "unit": "multiplicity" if integer else "F" if key.startswith("CAPACITOR") else "A",
                "sampling_scale": "log"}
        if integer:
            rule["integer"] = True
        task["parameters"][key] = rule
        task["initial_parameters"][key] = int(value) if integer else value
    return task


def latin_hypercube(task, count, seed):
    rng = random.Random(seed)
    columns = {}
    for name, rule in sorted(task["parameters"].items()):
        points = [(index + rng.random()) / count for index in range(count)]
        rng.shuffle(points)
        values = [math.exp(math.log(rule["min"]) + point * math.log(rule["max"] / rule["min"])) for point in points]
        if rule.get("integer"):
            values = [min(rule["max"], max(rule["min"], round(value))) for value in values]
        columns[name] = values
    return [{name: values[index] for name, values in columns.items()} for index in range(count)]


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 192
    probe = ROOT / "runs/benchmark_source_probe_20260910"
    names = [row["circuit"] for row in json.loads((probe / "summary.json").read_text())
             if row["status"] == "ok" or row["circuit"] == "Leung_NMCF_Pin_3"]
    cache = MeasurementCache(output / "measurements")
    jobs = []
    totals = {}
    summaries = {}
    for index, name in enumerate(sorted(names)):
        task = source_task(name, probe)
        save_json(output / "templates" / (name + ".json"), task)
        samples = [task["initial_parameters"], *latin_hypercube(task, count, 20260910 + index)]
        jobs.extend((name, sample_index, task, parameters) for sample_index, parameters in enumerate(samples))
        totals[name] = {"completed": 0, "valid": 0, "stable_60deg": 0, "planned": len(samples)}
        summaries[name] = []
    random.Random(20260910).shuffle(jobs)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(cache.measure, task, parameters): (name, index)
                   for name, index, task, parameters in jobs}
        for future in as_completed(pending):
            name, index = pending[future]
            result = future.result()
            record = {"sample_index": index, **{key: result[key] for key in
                      ("parameters", "status", "metrics", "measurement_key", "measurement_directory")},
                      "error": result.get("error")}
            summaries[name].append(record)
            total = totals[name]
            total["completed"] += 1
            total["valid"] += result["status"] == "ok"
            total["stable_60deg"] += result["status"] == "ok" and result["metrics"].get("phase_margin_deg", 0) >= 60
            save_json(output / "designs" / (name + ".json"), sorted(summaries[name], key=lambda row: row["sample_index"]))
            if sum(item["completed"] for item in totals.values()) % 50 == 0:
                print(json.dumps(totals), flush=True)
                save_json(output / "progress.json", totals)
    save_json(output / "progress.json", totals)
    print(json.dumps(totals), flush=True)


if __name__ == "__main__":
    main()
