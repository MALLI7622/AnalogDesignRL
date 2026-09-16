"""Measure controlled starting-design perturbations for a sizing curriculum.

Only the initial design changes. Requirements, device bounds, and the evaluator
stay fixed. Perturbation size and the number of edited controls are construction
metadata, never calibrated difficulty labels.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import math
from pathlib import Path

from analog_design.metrics import score
from analog_design.simulator import ROOT
from benchmark.calibrate import CODE_FILES, InputGuard
from benchmark.candidates import distance
from benchmark.explore import rounded_parameters
from benchmark.simulation import MeasurementCache, canonical_hash, save_json


ABSOLUTE_FAILURE = {"gain_db": 0.2, "unity_gain_hz": 10, "phase_margin_deg": 0.3,
                    "power_w": 1e-8, "dc_error_v": 4e-6, "max_tracking_error_v": 4e-6,
                    "settling_rise_s": 12e-9, "settling_fall_s": 12e-9}


def clear_failures(metrics, constraints):
    """Identify failures larger than the reference guard's numerical slack."""
    failures = []
    for name, limits in constraints.items():
        if name not in ABSOLUTE_FAILURE or len(limits) != 1:
            raise ValueError("Unsupported curriculum metric constraint.")
        direction, target = next(iter(limits.items()))
        value = metrics.get(name)
        if direction not in {"min", "max"} or any(
                isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) for number in (value, target)) or target <= 0:
            raise ValueError("Curriculum failures require finite metrics and positive targets.")
        violation = target - value if direction == "min" else value - target
        minimum = max(ABSOLUTE_FAILURE[name], 0.02 * target
                      if name not in {"gain_db", "phase_margin_deg"} else 0)
        if violation >= minimum:
            failures.append(name)
    return failures


def perturbations(task, reference, index, *, count=4):
    """Generate deterministic physical-value changes, without querying a solver.

    First try doubling/halving a capacitor, then a bias current. Optional further
    candidates jointly change capacitance and current by factors 1.5 and 2/3.
    These engineering changes do not depend on any baseline's search trajectory.
    """
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("Use one to eight perturbations per reference.")
    reference = rounded_parameters(task, reference)
    capacitors = sorted(name for name, rule in task["parameters"].items() if rule["unit"] == "F")
    currents = sorted(name for name, rule in task["parameters"].items() if rule["unit"] == "A")
    if not capacitors or not currents:
        raise ValueError("The current curriculum needs capacitor and bias-current controls.")
    cap, current = capacitors[index % len(capacitors)], currents[index % len(currents)]
    plans = [({cap: factor}, "single_capacitance") for factor in (2.0, 0.5)]
    plans += [({current: factor}, "single_bias") for factor in (2.0, 0.5)]
    plans += [({cap: first, current: second}, "capacitance_and_bias")
              for first, second in ((1.5, 1.5), (2 / 3, 2 / 3), (1.5, 2 / 3), (2 / 3, 1.5))]
    selected, seen = [], {canonical_hash(reference)}
    for factors, kind in plans[:count]:
        values = deepcopy(reference)
        for name, factor in factors.items():
            rule = task["parameters"][name]
            values[name] = min(rule["max"], max(rule["min"], reference[name] * factor))
        values = rounded_parameters(task, values)
        key = canonical_hash(values)
        if key in seen:
            continue
        seen.add(key)
        changed = [name for name in values if values[name] != reference[name]]
        selected.append({"parameters": values, "perturbation_kind": kind,
                         "requested_factors": factors, "changed_controls": changed,
                         "actual_factors": {name: values[name] / reference[name] for name in changed},
                         "normalized_reference_distance": distance(task, values, reference)})
    return selected


def characterize(target_file, output_directory, cache_directory, *, workers=4,
                 per_reference=4, maximum_per_topology=None):
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("One to eight workers are required.")
    if type(per_reference) is not int or not 1 <= per_reference <= 8:
        raise ValueError("Use one to eight perturbations per reference.")
    if maximum_per_topology is not None and (type(maximum_per_topology) is not int or maximum_per_topology < 1):
        raise ValueError("maximum_per_topology must be a positive integer when provided.")
    targets_path, output = Path(target_file).resolve(), Path(output_directory).resolve()
    catalog_path = ROOT / "benchmark/domains.json"
    guard = InputGuard([targets_path, catalog_path, Path(__file__), *[ROOT / path for path in CODE_FILES]])
    catalog = {entry["name"]: entry for entry in json.loads(catalog_path.read_text())["entries"]}
    target_data = json.loads(targets_path.read_text())
    targets = target_data["targets"] if isinstance(target_data, dict) else target_data
    if not isinstance(targets, list) or not targets:
        raise ValueError("Nonempty targets are required.")
    output.mkdir(parents=True, exist_ok=False)
    counts, jobs, seen_targets = {}, [], set()
    for target in targets:
        name, key = target["topology"], target["key"]
        if name not in catalog or not isinstance(key, str) or (name, key) in seen_targets:
            raise ValueError("Unknown topology or duplicate target identity.")
        seen_targets.add((name, key))
        index = counts.get(name, 0)
        if maximum_per_topology is not None and index >= maximum_per_topology:
            continue
        counts[name] = index + 1
        task = deepcopy(catalog[name]["task"])
        task["constraints"] = deepcopy(target["constraints"])
        for proposal in perturbations(task, target["parameters"], index, count=per_reference):
            jobs.append((len(jobs), name, key, task, proposal))
    guard.assert_unchanged()
    save_json(output / "plan.json", {"target_file": str(targets_path), "inputs_sha256": guard.hashes,
        "generation_mode": "controlled_physical_parameter_perturbations", "per_reference": per_reference,
        "target_counts": counts, "planned_measurements": len(jobs), "difficulty_status": "unmeasured"})
    cache = MeasurementCache(cache_directory, simulator_timeout=30)
    records, completed, eligible = {}, 0, 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {executor.submit(cache.evaluate, task, proposal["parameters"]): (sample_index, name, key, task, proposal)
                   for sample_index, name, key, task, proposal in jobs}
        for future in as_completed(pending):
            sample_index, name, key, task, proposal = pending[future]
            result = future.result()
            failures = clear_failures(result["metrics"], task["constraints"]) if result["status"] == "ok" else []
            native = clear_failures(result["independent_metrics"], task["constraints"]) if result["status"] == "ok" else []
            usable = result["status"] == "ok" and not result["success"] and bool(set(failures) & set(native))
            record = {"sample_index": sample_index, "target_key": key, **proposal,
                **{field: result[field] for field in ("status", "metrics", "measurement_key", "measurement_directory")},
                "error": result.get("error"), "eligible_failing_start": usable,
                "clear_failure_metrics": sorted(set(failures) & set(native)),
                "target_score": score(result["metrics"], task["constraints"]) if result["status"] == "ok" else None}
            records.setdefault(name, []).append(record)
            completed += 1
            eligible += usable
            save_json(output / "designs" / (name + ".json"), sorted(records[name], key=lambda row: (row["target_key"], canonical_hash(row["parameters"]))))
            if completed % 25 == 0 or completed == len(jobs):
                progress = {"completed": completed, "planned": len(jobs), "eligible_failing_starts": eligible,
                            "physical_evaluations": cache.physical_evaluations, "cache_hits": cache.cache_hits}
                save_json(output / "progress.json", progress)
                print(json.dumps(progress), flush=True)
    guard.assert_unchanged()
    cache.assert_unchanged()
    save_json(output / "completion.json", {"status": "complete", "measurements": completed,
              "eligible_failing_starts": eligible, "inputs_sha256": guard.hashes,
              "difficulty_status": "not_calibrated", "training_approved": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--per-reference", type=int, default=4)
    parser.add_argument("--maximum-per-topology", type=int)
    args = parser.parse_args()
    characterize(args.targets, args.output, args.cache, workers=args.workers,
                 per_reference=args.per_reference, maximum_per_topology=args.maximum_per_topology)


if __name__ == "__main__":
    main()
