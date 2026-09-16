"""Admit rounded sizing candidates and measure bounded baseline search behavior.

This is a calibration audit, not a training-approval or publication gate. Proposed
construction levels remain separate from the observed baseline success rates.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import threading
import time
import uuid

from analog_design.simulator import ROOT, digest, parameters_for
from benchmark.baselines import METHODS, run_search
from benchmark.candidates import reference_margin
from benchmark.explore import rounded_parameters
from benchmark.simulation import MeasurementCache, canonical_hash, measurement_identity, save_json


BUDGET = 30
PHYSICAL_FIELDS = ("circuit_directory", "subcircuit", "conditions", "parameters", "ac", "transient")
CODE_FILES = ("benchmark/calibrate.py", "benchmark/baselines.py", "benchmark/engineering_search.py",
              "benchmark/candidates.py",
              "benchmark/explore.py", "benchmark/catalog.py", "benchmark/simulation.py",
              "analog_design/simulator.py", "analog_design/metrics.py", "analog_design/crosscheck.py",
              "dependencies.lock.json")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def wilson_interval(successes, trials, z=1.959963984540054):
    """Two-sided 95% Wilson interval; no estimate when no trial was completed."""
    if type(successes) is not int or type(trials) is not int or not 0 <= successes <= trials:
        raise ValueError("Wilson counts must be integers with 0 <= successes <= trials.")
    if trials == 0:
        return None
    proportion = successes / trials
    denominator = 1 + z * z / trials
    center = (proportion + z * z / (2 * trials)) / denominator
    radius = z * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials)) / denominator
    return {"low": max(0.0, center - radius), "high": min(1.0, center + radius)}


def summarize_trials(trials):
    """Count full logical episodes; failures are right-censored at budget 30."""
    success_calls = [row["first_success_evaluation"] for row in trials if row["success"]]
    censored = [row["first_success_evaluation"] if row["success"] else BUDGET for row in trials]
    thresholds = {}
    for budget in (5, 10, 30):
        successes = sum(row["success"] and row["first_success_evaluation"] <= budget for row in trials)
        thresholds[str(budget)] = {"successes": successes, "trials": len(trials),
                                   "rate": successes / len(trials) if trials else None,
                                   "wilson_95": wilson_interval(successes, len(trials))}
    return {"completed_trials": len(trials), "successes": len(success_calls),
            "failures": len(trials) - len(success_calls), "success_at": thresholds,
            "median_calls_successes": statistics.median(success_calls) if success_calls else None,
            "median_calls_censored_at_30": statistics.median(censored) if censored else None,
            "logical_evaluations": sum(row["evaluations_used"] for row in trials)}


def save_checkpoint(path, identity, payload):
    """Atomic completed checkpoint; its identity and content are both verified."""
    if "_checkpoint" in payload:
        raise ValueError("Checkpoint metadata is reserved.")
    save_json(path, {**payload, "_checkpoint": {
        "schema_version": 1, "complete": True, "identity": identity,
        "identity_sha256": canonical_hash(identity), "payload_sha256": canonical_hash(payload)}})


def load_checkpoint(path, identity):
    path = Path(path)
    if not path.is_file():
        return None  # A partially populated task directory is resumable.
    record = json.loads(path.read_text())
    metadata = record.get("_checkpoint", {})
    if (metadata.get("schema_version") != 1 or metadata.get("identity") != identity
            or metadata.get("identity_sha256") != canonical_hash(identity)):
        raise ValueError(f"Checkpoint identity changed; use a new output directory: {path}")
    if metadata.get("complete") is not True:
        return None
    payload = {key: value for key, value in record.items() if key != "_checkpoint"}
    if metadata.get("payload_sha256") != canonical_hash(payload):
        raise ValueError(f"Checkpoint content hash disagrees: {path}")
    return payload


def admission_decision(task, initial, reference, public_seed):
    """Require a valid failing start, a robust passing reference, and no seed win."""
    reasons = []
    start_ok = initial.get("status") == "ok" and initial.get("success") is False
    reference_ok = reference.get("status") == "ok" and reference.get("success") is True
    primary_margin = reference_ok and reference_margin(reference["metrics"], task["constraints"])
    native_margin = reference_ok and reference_margin(reference["independent_metrics"], task["constraints"])
    seed_passes = public_seed.get("status") == "ok" and public_seed.get("success") is True
    if not start_ok:
        reasons.append("Initial design must simulate validly and fail at least one requirement.")
    if not reference_ok:
        reasons.append("Reference must simulate validly and pass every requirement.")
    elif not primary_margin or not native_margin:
        reasons.append("Reference does not satisfy the required slack in both measurement paths.")
    if seed_passes:
        reasons.append("The released public seed already solves this task.")
    return {"admitted": not reasons, "rejection_reasons": reasons,
            "reference_margin": {"python": bool(primary_margin), "native": bool(native_margin)},
            "public_seed_status": "passes" if seed_passes else "valid_failure" if public_seed.get("status") == "ok" else "invalid"}


class InputGuard:
    def __init__(self, paths):
        self.hashes = {str(Path(path).resolve()): digest(path) for path in paths}

    def assert_unchanged(self):
        for filename, expected in self.hashes.items():
            if not Path(filename).is_file() or digest(filename) != expected:
                raise RuntimeError("Calibration input changed: " + filename)


def _read_ids(path):
    if path is None:
        return None
    text = Path(path).read_text().strip()
    if text.startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("ids", data.get("tasks"))
        if not isinstance(data, list):
            raise ValueError("IDs JSON must be a list, or an object with ids/tasks.")
        ids = [item.get("id") if isinstance(item, dict) else item for item in data]
    else:
        ids = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not ids or any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("The IDs file must contain distinct nonempty task IDs.")
    return set(ids)


def load_candidates(directory, catalog, *, names=None, ids=None):
    directory = Path(directory).resolve()
    index = json.loads((directory / "index.json").read_text())
    entries = index.get("tasks", index.get("entries"))
    if not isinstance(entries, list):
        raise ValueError("Candidate index must contain a tasks list.")
    domains = {entry["name"]: entry for entry in catalog["entries"]}
    if names and set(names) - set(domains):
        raise ValueError("Unknown topology names: " + ", ".join(sorted(set(names) - set(domains))))
    records, seen = [], set()
    for entry in entries:
        task_id = entry["id"]
        if task_id in seen:
            raise ValueError("Duplicate candidate ID: " + task_id)
        seen.add(task_id)
        if names and entry["topology"] not in names or ids is not None and task_id not in ids:
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
            raise ValueError("Invalid task ID: " + task_id)
        path = (directory / entry["path"]).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("Candidate path escapes input directory.")
        candidate = json.loads(path.read_text())
        task = deepcopy(candidate["task"])
        if candidate["id"] != task_id or task["id"] != task_id or candidate["topology"] != entry["topology"]:
            raise ValueError("Candidate identity disagrees with index: " + task_id)
        if type(task.get("max_evaluations")) is not int or task["max_evaluations"] != BUDGET:
            raise ValueError("Calibration requires the fixed 30-evaluation contract.")
        domain = domains[candidate["topology"]]
        for field in PHYSICAL_FIELDS:
            if canonical_hash(task[field]) != canonical_hash(domain["task"][field]):
                raise ValueError(f"Candidate {task_id} differs from its trusted domain in {field}.")
        for label, values in (("initial", task["initial_parameters"]), ("reference", candidate["reference"]),
                              ("public_default", domain["public_default"])):
            if set(values) != set(task["parameters"]):
                raise ValueError(f"{task_id}: {label} does not contain the full parameter vector.")
            parameters_for(task, values)
        task["initial_parameters"] = rounded_parameters(task, task["initial_parameters"])
        reference = rounded_parameters(task, candidate["reference"])
        records.append({"candidate": candidate, "task": task, "reference": reference,
                        "public_default": deepcopy(domain["public_default"]), "path": path})
    if ids is not None and ids - {record["candidate"]["id"] for record in records}:
        raise ValueError("Requested IDs were missing or excluded by topology filter: " +
                         ", ".join(sorted(ids - {record["candidate"]["id"] for record in records})))
    if not records:
        raise ValueError("No candidates match the requested selection.")
    return sorted(records, key=lambda record: record["candidate"]["id"])


def make_summary(records, admissions, trials, *, methods, seeds, accounting, complete):
    all_trials = list(trials.values())
    by_task, by_task_method, by_method = {}, {}, {}
    for trial in all_trials:
        task_id, method = trial["task_id"], trial["method"]
        by_task.setdefault(task_id, []).append(trial)
        by_task_method.setdefault((task_id, method), []).append(trial)
        by_method.setdefault(method, []).append(trial)
    tasks = []
    for record in records:
        candidate = record["candidate"]
        task_id = candidate["id"]
        admission = admissions.get(task_id)
        task_trials = by_task.get(task_id, [])
        per_method = {method: summarize_trials(by_task_method.get((task_id, method), []))
                      for method in methods}
        tasks.append({"id": task_id, "topology": candidate["topology"],
                      "topology_family": candidate.get("topology_family"),
                      "proposed_difficulty": candidate.get("proposed_difficulty"),
                      "profile": candidate.get("profile"),
                      "admitted": admission["admitted"] if admission else None,
                      "rejection_reasons": admission["rejection_reasons"] if admission else [],
                      "expected_trials": len(methods) * len(seeds) if admission and admission["admitted"] else 0,
                      "methods": per_method, "pooled": summarize_trials(task_trials)})
    expected = sum(task["expected_trials"] for task in tasks)
    return {"schema_version": 1, "status": "complete" if complete else "partial",
            "updated_utc": utc_now(), "budget": BUDGET, "methods": methods, "seeds": seeds,
            "task_count": len(records), "admission_count": len(admissions),
            "admitted_count": sum(row["admitted"] for row in admissions.values()),
            "rejected_count": sum(not row["admitted"] for row in admissions.values()),
            "expected_trials": expected, "completed_trials": len(all_trials),
            "remaining_trials": expected - len(all_trials), "tasks": tasks,
            "per_method": {method: summarize_trials(by_method.get(method, []))
                           for method in methods},
            "pooled": summarize_trials(all_trials), "accounting": accounting,
            "difficulty_status": "baseline_observations_only_no_absolute_difficulty_assignment",
            "interpretation": {
                "success_at": "Fraction of completed seed trials first succeeding by evaluation 5, 10, or 30; the initial design consumes evaluation 1.",
                "censoring": "Unsuccessful trials are assigned 30 for the censored median, including early domain exhaustion. This is a capped descriptive statistic, not estimated uncensored solve time.",
                "intervals": "95% binomial Wilson intervals over seed trials. Shared tasks/topologies induce dependence; pooled intervals are descriptive and do not establish out-of-distribution generalization.",
                "construction_levels": "proposed_difficulty is copied from candidate construction and is not a calibrated difficulty label.",
                "invalid_public_seed": "An invalid released seed is permitted and recorded; every admitted start and private reference must still simulate validly.",
            }, "training_approved": False}


def calibrate(candidate_directory, output_directory, cache_directory, *, catalog_path=None,
              methods=METHODS, seeds=(0, 1, 2), workers=4, names=None, ids_file=None):
    methods, seeds = list(methods), list(seeds)
    if not methods or len(set(methods)) != len(methods) or set(methods) - set(METHODS):
        raise ValueError("Methods must be distinct supported baselines.")
    if not seeds or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Seeds must be distinct integers.")
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("Use between 1 and 8 workers.")
    candidates = Path(candidate_directory).resolve()
    catalog_path = Path(catalog_path or ROOT / "benchmark/domains.json").resolve()
    output = Path(output_directory).resolve()
    records = load_candidates(candidates, json.loads(catalog_path.read_text()),
                              names=names, ids=_read_ids(ids_file))
    paths = [candidates / "index.json", catalog_path, *[ROOT / path for path in CODE_FILES],
             *[record["path"] for record in records]]
    if ids_file:
        paths.append(Path(ids_file).resolve())
    guard = InputGuard(paths)
    output.mkdir(parents=True, exist_ok=True)
    cache = MeasurementCache(cache_directory, simulator_timeout=30)
    physical_identities = {}
    for record in records:
        physical = canonical_hash({key: record["task"][key] for key in PHYSICAL_FIELDS})
        if physical not in physical_identities:
            with cache.lock:
                physical_identities[physical] = measurement_identity(record["task"], snapshot=cache.snapshot,
                                                                     simulator_timeout=cache.timeout)
        record["physics_sha256"] = canonical_hash(physical_identities[physical])
    config = {"methods": methods, "seeds": seeds, "budget": BUDGET, "simulator_timeout_s": 30,
              "candidate_ids": [record["candidate"]["id"] for record in records],
              "inputs_sha256": guard.hashes, "cache_directory": str(cache.directory),
              "physics": {key: canonical_hash(value) for key, value in physical_identities.items()}}
    config_hash = canonical_hash(config)
    manifest_path = output / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("configuration") != config or manifest.get("configuration_sha256") != config_hash:
            raise ValueError("Calibration configuration or input hashes changed; use a new output directory.")
    else:
        save_json(manifest_path, {"schema_version": 1, "created_utc": utc_now(),
                                 "configuration": config, "configuration_sha256": config_hash,
                                 "physical_identities": physical_identities})
    for record in records:
        record["checkpoint_identity"] = {"configuration_sha256": config_hash,
            "candidate_sha256": digest(record["path"]), "task_sha256": canonical_hash(record["task"]),
            "reference_sha256": canonical_hash(record["reference"]),
            "public_default_sha256": canonical_hash(record["public_default"]),
            "physics_sha256": record["physics_sha256"]}
    guard.assert_unchanged()
    cache.assert_unchanged()
    admissions, trials = {}, {}
    counts = Counter()
    counts_lock = threading.Lock()
    started = time.monotonic()
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:12]
    session_path = output / "sessions" / (session_id + ".json")
    session_status = "running"

    def observe(task, parameters, phase):
        with counts_lock:
            counts[phase + "_logical_calls_this_invocation"] += 1
        return cache.evaluate(task, parameters)

    def admit(record):
        task_id = record["candidate"]["id"]
        identity = {**record["checkpoint_identity"], "kind": "admission"}
        path = output / task_id / "admission.json"
        existing = load_checkpoint(path, identity)
        if existing is not None:
            with counts_lock:
                counts["admission_checkpoints_reused"] += 1
            return existing
        task = record["task"]
        initial = observe(task, task["initial_parameters"], "admission")
        reference = observe(task, record["reference"], "admission")
        public_seed = observe(task, record["public_default"], "admission")
        admission = {"id": task_id, "topology": record["candidate"]["topology"],
                     "checked_utc": utc_now(), "effective_task": task,
                     "rounded_reference_parameters": record["reference"],
                     "public_default_parameters": record["public_default"],
                     **admission_decision(task, initial, reference, public_seed),
                     "logical_evaluations": 3,
                     "evaluations": {"initial": initial, "reference": reference, "public_seed": public_seed}}
        save_checkpoint(path, identity, admission)
        return admission

    def run_trial(record, method, seed):
        task_id = record["candidate"]["id"]
        identity = {**record["checkpoint_identity"], "kind": "baseline", "method": method, "seed": seed}
        path = output / task_id / f"{method}_{seed}.json"
        existing = load_checkpoint(path, identity)
        if existing is not None:
            with counts_lock:
                counts["trial_checkpoints_reused"] += 1
            return existing
        measurements = []
        task = record["task"]

        def evaluate_action(parameters):
            # Neither candidate.reference nor admission results enter the solver.
            result = observe(task, parameters, "baseline")
            measurements.append({"measurement_key": result["measurement_key"],
                                 "measurement_directory": result["measurement_directory"],
                                 "independent_metrics": result.get("independent_metrics", {}),
                                 "crosscheck": result.get("crosscheck", {}), "error": result.get("error")})
            return result

        trial = run_search(task, evaluate_action, method, seed, budget=BUDGET)
        if len(measurements) != len(trial["trajectory"]):
            raise RuntimeError("Baseline trajectory and physical-query trace disagree.")
        for step, measurement in zip(trial["trajectory"], measurements):
            step.update(measurement)
        trial["completed_utc"] = utc_now()
        save_checkpoint(path, identity, trial)
        return trial

    def accounting():
        with counts_lock:
            logical_counts = dict(counts)
        current = {"session_id": session_id, "status": session_status, "workers": workers,
                   "elapsed_seconds": time.monotonic() - started,
                   "physical_evaluations_this_invocation": cache.physical_evaluations,
                   "cache_hits_this_invocation": cache.cache_hits, **logical_counts}
        save_json(session_path, current)
        sessions = [json.loads(path.read_text()) for path in (output / "sessions").glob("*.json")]
        return {"current_invocation": current,
                "recorded_physical_evaluations_all_invocations": sum(row["physical_evaluations_this_invocation"] for row in sessions),
                "recorded_baseline_callback_calls_all_invocations": sum(row.get("baseline_logical_calls_this_invocation", 0) for row in sessions),
                "recorded_admission_callback_calls_all_invocations": sum(row.get("admission_logical_calls_this_invocation", 0) for row in sessions),
                "admission_logical_evaluations_in_checkpoints": sum(row["logical_evaluations"] for row in admissions.values()),
                "baseline_logical_evaluations_in_checkpoints": sum(row["evaluations_used"] for row in trials.values()),
                "physical_definition": "One physical evaluation invokes the controlled AC/DC and transient evaluator; it may fail before completing both simulator invocations.",
                "resume_definition": "Completed checkpoints retain their logical budgets but incur zero new callbacks. Recomputed partial trials incur callbacks again; measurement caching can avoid physical work.",
                "accounting_limit": "Session accounting is checkpointed with progress. Abrupt process termination can leave unrecorded work after the last progress checkpoint."}

    def write_summary(complete=False):
        summary = make_summary(records, admissions, trials, methods=methods, seeds=seeds,
                               accounting=accounting(), complete=complete)
        save_json(output / "summary.json", summary)
        return summary

    try:
        write_summary()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(admit, record): record for record in records}
            for future in as_completed(futures):
                record = futures[future]
                admission = future.result()  # Infrastructure errors remain visible.
                admissions[record["candidate"]["id"]] = admission
                if len(admissions) % 10 == 0 or len(admissions) == len(records):
                    write_summary()
                    print(json.dumps({"phase": "admission", "completed": len(admissions),
                                      "total": len(records), "admitted": sum(row["admitted"] for row in admissions.values())}), flush=True)
        guard.assert_unchanged()
        cache.assert_unchanged()
        jobs = [(record, method, seed) for record in records if admissions[record["candidate"]["id"]]["admitted"]
                for method in methods for seed in seeds]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_trial, *job): job for job in jobs}
            for future in as_completed(futures):
                record, method, seed = futures[future]
                trial = future.result()
                trials[(record["candidate"]["id"], method, seed)] = trial
                if len(trials) % 10 == 0 or len(trials) == len(jobs):
                    write_summary()
                    print(json.dumps({"phase": "baselines", "completed": len(trials), "total": len(jobs),
                                      "physical_evaluations": cache.physical_evaluations,
                                      "logical_evaluations": sum(row["evaluations_used"] for row in trials.values())}), flush=True)
        guard.assert_unchanged()
        cache.assert_unchanged()
        session_status = "complete"
        return write_summary(complete=True)
    except BaseException:
        session_status = "failed"
        write_summary()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=ROOT / "runs/benchmark_measurements_v1")
    parser.add_argument("--catalog", type=Path, default=ROOT / "benchmark/domains.json")
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=list(METHODS))
    parser.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--names", nargs="+")
    parser.add_argument("--ids-file", type=Path)
    args = parser.parse_args()
    result = calibrate(args.candidates, args.output, args.cache, catalog_path=args.catalog,
                       methods=args.methods, seeds=args.seeds, workers=args.workers,
                       names=args.names, ids_file=args.ids_file)
    print(json.dumps({key: result[key] for key in ("status", "task_count", "admitted_count", "rejected_count", "completed_trials")}), flush=True)


if __name__ == "__main__":
    main()
