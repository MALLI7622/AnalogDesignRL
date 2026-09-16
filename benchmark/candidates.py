"""Construct auditable sizing candidates from measured, source-grounded designs.

Proposed levels describe construction only. The calibration stage assigns actual
baseline-relative difficulty; this module never labels a task as calibrated.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import random

from analog_design.metrics import score
from analog_design.simulator import ROOT, digest
from benchmark.explore import rounded_parameters
from benchmark.simulation import canonical_hash, save_json


METRICS = ("gain_db", "unity_gain_hz", "phase_margin_deg", "power_w", "dc_error_v",
           "max_tracking_error_v", "settling_rise_s", "settling_fall_s")

GRIDS = {
    "gain_db": list(range(50, 141, 5)),
    "unity_gain_hz": [factor * 10 ** exponent for exponent in range(4, 8) for factor in (1, 1.5, 2, 3, 5, 7.5)],
    "phase_margin_deg": [60, 65, 70, 75, 80, 85],
    "power_w": [factor * 10 ** exponent for exponent in range(-6, -2) for factor in (1, 1.5, 2, 3, 5, 7.5)],
    "dc_error_v": [factor * 10 ** exponent for exponent in range(-4, -1) for factor in (1, 1.5, 2, 3, 5, 7.5)],
    "max_tracking_error_v": [factor * 10 ** exponent for exponent in range(-4, -1) for factor in (1, 1.5, 2, 3, 5, 7.5)],
    "settling_rise_s": [factor * 10 ** exponent for exponent in range(-7, -4) for factor in (1, 1.5, 2, 3, 5, 7.5)],
    "settling_fall_s": [factor * 10 ** exponent for exponent in range(-7, -4) for factor in (1, 1.5, 2, 3, 5, 7.5)],
}


def requirement_identity(task):
    return {key: task[key] for key in ("circuit_directory", "subcircuit", "conditions", "parameters",
                                      "ac", "transient", "constraints", "max_evaluations")}


def distance(task, a, b):
    differences = [math.log(a[name] / b[name]) / math.log(rule["max"] / rule["min"])
                   for name, rule in task["parameters"].items()]
    return math.sqrt(sum(value * value for value in differences) / len(differences))


def reference_margin(metrics, constraints):
    absolute = {"gain_db": 0.2, "unity_gain_hz": 10, "phase_margin_deg": 0.3,
                "power_w": 1e-8, "dc_error_v": 4e-6, "max_tracking_error_v": 4e-6,
                "settling_rise_s": 1.2e-8, "settling_fall_s": 1.2e-8}
    for name, limits in constraints.items():
        if name not in absolute or name not in metrics or len(limits) != 1:
            return False
        direction, target = next(iter(limits.items()))
        if direction not in {"min", "max"} or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                                  or not math.isfinite(value) for value in (metrics[name], target)) or target <= 0:
            return False
        slack = metrics[name] - target if direction == "min" else target - metrics[name]
        required = max(absolute[name], 0.02 * target if name not in {"gain_db", "phase_margin_deg"} else 0)
        if slack < required:
            return False
    return True


def propose_constraints(task, metrics, level, profile):
    factors = {"easy": (12, 0.35, 15, 1.8, 2.0, 2.0),
               "medium": (6, 0.65, 10, 1.35, 1.5, 1.5),
               "hard": (1.5, 0.88, 3, 1.10, 1.15, 1.2)}[level]
    gain_slack, speed_fraction, phase_slack, power_factor, error_factor, settling_factor = factors
    wanted = {"gain_db": metrics["gain_db"] - gain_slack,
              "unity_gain_hz": metrics["unity_gain_hz"] * speed_fraction,
              "phase_margin_deg": metrics["phase_margin_deg"] - phase_slack,
              "power_w": metrics["power_w"] * power_factor,
              "dc_error_v": max(1e-4, metrics["dc_error_v"] * error_factor),
              "max_tracking_error_v": max(1e-4, metrics["max_tracking_error_v"] * error_factor),
              "settling_rise_s": metrics["settling_rise_s"] * settling_factor,
              "settling_fall_s": metrics["settling_fall_s"] * settling_factor}
    focus = {"balanced": set(METRICS), "speed_power": {"unity_gain_hz", "power_w"},
             "gain_accuracy": {"gain_db", "dc_error_v", "max_tracking_error_v"},
             "stability_settling": {"phase_margin_deg", "settling_rise_s", "settling_fall_s"}}[profile]
    constraints = deepcopy(task["constraints"])
    for name in METRICS:
        direction, floor = next(iter(constraints[name].items()))
        if name not in focus:
            continue
        candidates = [value for value in GRIDS[name] if value <= wanted[name]] if direction == "min" else [value for value in GRIDS[name] if value >= wanted[name]]
        if not candidates:
            continue
        value = max(candidates) if direction == "min" else min(candidates)
        constraints[name] = {direction: max(floor, value) if direction == "min" else min(floor, value)}
    return constraints


def load_designs(directories, entry):
    designs = {}
    task = entry["task"]
    circuit = ROOT / task["circuit_directory"]
    expected = {"netlist_sha256": digest(circuit / "netlist.spice"),
                "reference_parameters_sha256": digest(circuit / "reference.params"),
                "evaluator_sha256": digest(ROOT / "analog_design/simulator.py"),
                "metrics_code_sha256": digest(ROOT / "analog_design/metrics.py"),
                "crosscheck_code_sha256": digest(ROOT / "analog_design/crosscheck.py")}
    for directory in directories:
        path = Path(directory) / (entry["name"] + ".json")
        if not path.is_file():
            continue
        for row in json.loads(path.read_text()):
            if row["status"] != "ok" or set(METRICS) - row["metrics"].keys():
                continue
            evidence_directory = (ROOT / row["measurement_directory"]).resolve()
            if not evidence_directory.is_relative_to(ROOT / "runs"):
                raise ValueError("Characterization evidence is outside the trusted runs directory.")
            evidence = json.loads((evidence_directory / "result.json").read_text())
            measured_task = json.loads((evidence_directory / "task.json").read_text())
            if (evidence.get("status") != "ok" or evidence.get("metrics") != row["metrics"]
                    or evidence.get("parameters") != row["parameters"]
                    or any(evidence.get("provenance", {}).get(key) != value for key, value in expected.items())
                    or any(measured_task[key] != task[key] for key in ("subcircuit", "conditions", "ac", "transient"))):
                raise ValueError("Characterization source, settings, or measurements changed: " + str(evidence_directory))
            parameters = {**entry["public_default"], **row["parameters"]}
            if set(parameters) != set(task["parameters"]):
                continue
            if any(not rule["min"] <= parameters[name] <= rule["max"] for name, rule in task["parameters"].items()):
                continue
            copy = {**row, "parameters": parameters}
            designs[canonical_hash(parameters)] = copy
    return list(designs.values())


def create_candidates(entry, designs):
    template = entry["task"]
    defaults = rounded_parameters(template, entry["public_default"])
    default_rows = [row for row in designs if rounded_parameters(template, row["parameters"]) == defaults]
    if not default_rows:
        raise ValueError("Missing measured public default for " + entry["name"])
    default_metrics = default_rows[0]["metrics"]
    references = [row for row in designs if reference_margin(row["metrics"], template["constraints"])
                  and distance(template, row["parameters"], defaults) >= 0.04]
    rng = random.Random(int(canonical_hash(entry["name"])[:8], 16))
    rng.shuffle(references)
    seen_groups = set()
    candidates = []
    for reference in references:
        for level in ("easy", "medium", "hard"):
            for profile in ("balanced", "speed_power", "gain_accuracy", "stability_settling"):
                constraints = propose_constraints(template, reference["metrics"], level, profile)
                if not reference_margin(reference["metrics"], constraints):
                    continue
                if score(default_metrics, constraints)["success"]:
                    continue
                task = deepcopy(template)
                task["constraints"] = constraints
                group = canonical_hash(requirement_identity(task))
                if group in seen_groups:
                    continue
                starts = []
                for start in designs:
                    result = score(start["metrics"], constraints)
                    if result["success"] or distance(template, start["parameters"], reference["parameters"]) < 0.04:
                        continue
                    failed = sum(not value for value in result["checks"].values())
                    dist = distance(template, start["parameters"], reference["parameters"])
                    if level == "easy":
                        priority = (failed != 1, dist, -result["reward"])
                    elif level == "medium":
                        priority = (abs(failed - 2), abs(dist - 0.22), -result["reward"])
                    else:
                        priority = (failed < 2, -dist, result["reward"])
                    starts.append((priority, start, result))
                if not starts:
                    continue
                _, start, initial_score = min(starts, key=lambda item: item[0])
                task["initial_parameters"] = rounded_parameters(template, start["parameters"])
                task["purpose"] = (f"Size the {entry['name']} amplifier by editing the permitted parameters. "
                                   f"Meet every fixed DC, AC, and transient requirement within 30 evaluations. "
                                   f"This instance emphasizes {profile.replace('_', ' ')}.")
                fingerprint = canonical_hash({**requirement_identity(task), "initial_parameters": task["initial_parameters"]})
                task["id"] = entry["name"].lower().replace("_pin_3", "") + "_bench_" + fingerprint[:16]
                feasible_designs = sorted(canonical_hash(row["parameters"]) for row in designs
                                          if score(row["metrics"], constraints)["success"])
                candidates.append({"id": task["id"], "topology": entry["name"], "topology_family": entry["family"],
                                   "proposed_difficulty": level, "profile": profile, "task": task,
                                   "reference": rounded_parameters(template, reference["parameters"]),
                                   "requirement_group": group, "fingerprint": fingerprint,
                                   "characterization_reference": reference["measurement_directory"],
                                   "characterization_start": start["measurement_directory"],
                                   "characterization_initial_failures": [name for name, passed in initial_score["checks"].items() if not passed],
                                   "empirical_pool_feasible_count": len(feasible_designs),
                                   "empirical_pool_valid_count": len(designs),
                                   "empirical_solution_signature": canonical_hash(feasible_designs),
                                   "generation_mode": "source_grounded_simulation_guided_deterministic_curation",
                                   "calibration_status": "pending", "training_approved": False})
                seen_groups.add(group)
    return candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "benchmark/domains.json")
    parser.add_argument("--designs", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-topology", type=int, default=90)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    entries = json.loads(args.catalog.read_text())["entries"]
    index = []
    for entry in entries:
        designs = load_designs(args.designs, entry)
        candidates = create_candidates(entry, designs)
        # Round-robin levels, profiles and references; avoid early-file bias.
        selected = []
        references = Counter()
        buckets = {(level, profile): [c for c in candidates if c["proposed_difficulty"] == level and c["profile"] == profile]
                   for level in ("easy", "medium", "hard")
                   for profile in ("balanced", "speed_power", "gain_accuracy", "stability_settling")}
        while len(selected) < args.per_topology and any(buckets.values()):
            for bucket in buckets.values():
                if not bucket or len(selected) >= args.per_topology:
                    continue
                best = min(range(len(bucket)), key=lambda i: references[canonical_hash(bucket[i]["reference"])])
                candidate = bucket.pop(best)
                references[canonical_hash(candidate["reference"])] += 1
                selected.append(candidate)
        for candidate in selected:
            relative = "candidates/" + candidate["id"] + ".json"
            save_json(args.output / relative, candidate)
            index.append({key: candidate[key] for key in ("id", "topology", "topology_family", "proposed_difficulty", "profile")})
            index[-1]["path"] = relative
        print(json.dumps({"topology": entry["name"], "valid_designs": len(designs), "available_candidates": len(candidates),
                          "selected_candidates": len(selected), "distinct_reference_vectors": len(references)}), flush=True)
    save_json(args.output / "index.json", {"status": "candidates_not_verified_or_calibrated", "tasks": index})


if __name__ == "__main__":
    main()
