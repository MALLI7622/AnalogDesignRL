"""Read-only coverage audit of immutable completed baseline trial checkpoints.

No simulator, cache writes, changing summary.json, or release qualification.
Optional --selection restricts target sets, while all completed eligible trials
remain the witness pool. This script writes only its requested research output.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from analog_design.metrics import score
from benchmark.calibrate import PHYSICAL_FIELDS, admission_decision, load_checkpoint
from benchmark.explore import rounded_parameters
from benchmark.release import InputSnapshot, validate_evaluation, validate_trial
from benchmark.simulation import canonical_hash, save_json
from benchmark.verify_release import _hash


def covering(masks, target_keys):
    masks = sorted(masks.items())
    worst, worst_mask = min(masks, key=lambda row: (-row[1].bit_count(), row[0]), default=(None, 0))
    remaining, steps = (1 << len(target_keys)) - 1, []
    while remaining:
        key, mask = min(masks, key=lambda row: (-(row[1] & remaining).bit_count(), row[0]), default=(None, 0))
        added = (mask & remaining).bit_count()
        if not added:
            break
        steps.append({"witness_key": key, "newly_covered": added})
        remaining &= ~mask
    return {"witness_count": len(masks), "target_count": len(target_keys),
            "maximum_covered_targets": worst_mask.bit_count(),
            "maximum_coverage_fraction": worst_mask.bit_count() / len(target_keys) if target_keys else None,
            "worst_witness_key": worst,
            "worst_witness_target_keys": [key for i, key in enumerate(target_keys) if worst_mask & (1 << i)],
            "coverage_histogram": dict(sorted(Counter(mask.bit_count() for _, mask in masks).items())),
            "greedy_cover_count": len(steps) if not remaining else None,
            "greedy_partial_cover_count": len(steps), "covered_targets": len(target_keys) - remaining.bit_count(),
            "uncovered_target_keys": [key for i, key in enumerate(target_keys) if remaining & (1 << i)],
            "greedy_steps": steps}


def _admission(snapshot, path, candidate_path, candidate, task, reference, domain, manifest):
    """Validate the complete identity and decision before trusting admitted."""
    physical_key = canonical_hash({key: task[key] for key in PHYSICAL_FIELDS})
    identity = {"configuration_sha256": manifest["configuration_sha256"],
                "candidate_sha256": snapshot.hashes[str(candidate_path)],
                "task_sha256": canonical_hash(task), "reference_sha256": canonical_hash(reference),
                "public_default_sha256": canonical_hash(domain["public_default"]),
                "physics_sha256": manifest["configuration"]["physics"][physical_key]}
    if not path.is_file():
        return None, identity, "missing"
    snapshot.read(path)
    record = load_checkpoint(path, {**identity, "kind": "admission"})
    if record is None:
        return None, identity, "incomplete"
    if (type(record.get("admitted")) is not bool or record.get("id") != candidate["id"]
            or record.get("topology") != candidate["topology"] or record.get("effective_task") != task
            or record.get("rounded_reference_parameters") != reference
            or record.get("public_default_parameters") != domain["public_default"]
            or record.get("logical_evaluations") != 3):
        raise ValueError("Admission payload differs from its complete task/reference/default identity.")
    evaluations = record.get("evaluations", {})
    if set(evaluations) != {"initial", "reference", "public_seed"}:
        raise ValueError("Admission needs all three recorded evaluations.")
    for result in evaluations.values():
        validate_evaluation(result, task)
    decision = admission_decision(task, **evaluations)
    if any(record.get(key) != value for key, value in decision.items()):
        raise ValueError("Admission flag differs from independently recomputed saved decisions.")
    return record, identity, "admitted" if decision["admitted"] else "rejected"


def run(candidates, calibrations, names, *, selection=None, allow_partial=False):
    candidates = Path(candidates).resolve()
    snapshot = InputSnapshot()
    snapshot.capture(Path(__file__))
    index = snapshot.read(candidates / "index.json")
    targets = snapshot.read(candidates / "targets.json")["targets"]
    curation = snapshot.read(candidates / "curation_report.json")
    catalog_path = (ROOT / "benchmark/domains.json").resolve()
    catalog = snapshot.read(catalog_path)
    domains = {entry["name"]: entry for entry in catalog["entries"]}
    entries = {row["id"]: row for row in index["tasks"]}
    source_targets = {(row["topology"], row["key"]): row for row in targets}
    selected_keys = None
    if selection is not None:
        selected = snapshot.read(selection)
        selected = selected.get("ids", selected.get("tasks")) if isinstance(selected, dict) else selected
        ids = [row["id"] if isinstance(row, dict) else row for row in selected]
        selected_keys = set()
        for task_id in ids:
            candidate = snapshot.read(candidates / entries[task_id]["path"])
            selected_keys.add((candidate["topology"], candidate["target_key"]))
    manifests = []
    for directory in calibrations:
        directory = Path(directory).resolve()
        manifest = snapshot.read(directory / "manifest.json")
        if canonical_hash(manifest["configuration"]) != manifest["configuration_sha256"]:
            raise ValueError("Calibration manifest configuration hash mismatch.")
        declared = manifest["configuration"]["inputs_sha256"]
        for path in (catalog_path, candidates / "index.json"):
            if declared.get(str(path)) != snapshot.hashes[str(path)]:
                raise ValueError("Calibration manifest does not bind the candidate index and trusted domains.")
        manifests.append((directory, manifest))
    outputs = {}
    for topology in names:
        chosen_targets = [row for row in targets if row["topology"] == topology and
                          (selected_keys is None or (topology, row["key"]) in selected_keys)]
        keys = [row["key"] for row in chosen_targets]
        pending, present, ids = [], [], set()
        admitted_ids, rejected_ids = set(), set()
        admissions, stages, pending_admissions = Counter(), [], 0
        rejected_trial_files_ignored = 0
        for directory, manifest in manifests:
            config = manifest["configuration"]
            stage_counts = Counter()
            for task_id in config["candidate_ids"]:
                if task_id not in entries or entries[task_id]["topology"] != topology:
                    continue
                ids.add(task_id)
                candidate_path = (candidates / entries[task_id]["path"]).resolve()
                if not candidate_path.is_relative_to(candidates):
                    raise ValueError("Candidate path escapes its indexed directory.")
                candidate = snapshot.read(candidate_path)
                if config["inputs_sha256"].get(str(candidate_path)) != snapshot.hashes[str(candidate_path)]:
                    raise ValueError("Candidate hash disagrees with its calibration input binding.")
                if candidate["id"] != task_id or candidate["task"]["id"] != task_id or candidate["topology"] != topology:
                    raise ValueError("Candidate identity disagrees with its index.")
                task = deepcopy(candidate["task"])
                task["initial_parameters"] = rounded_parameters(task, task["initial_parameters"])
                reference = rounded_parameters(task, candidate["reference"])
                domain = domains[topology]
                if any(canonical_hash(task[key]) != canonical_hash(domain["task"][key]) for key in PHYSICAL_FIELDS):
                    raise ValueError("Candidate differs from the frozen circuit/measurement domain.")
                target = source_targets[(topology, candidate["target_key"])]
                if task["constraints"] != target["constraints"] or reference != target["parameters"]:
                    raise ValueError("Candidate differs from its frozen target definition.")
                admission, identity, admission_status = _admission(snapshot, directory / task_id / "admission.json",
                    candidate_path, candidate, task, reference, domain, manifest)
                admissions[admission_status] += 1
                stage_counts[admission_status] += 1
                if admission is None:
                    pending_admissions += 1
                    continue
                if not admission["admitted"]:
                    rejected_ids.add(task_id)
                    rejected_trial_files_ignored += sum((directory / task_id / f"{method}_{seed}.json").is_file()
                                                       for method in config["methods"] for seed in config["seeds"])
                    continue
                admitted_ids.add(task_id)
                for method in config["methods"]:
                    for seed in config["seeds"]:
                        path = directory / task_id / f"{method}_{seed}.json"
                        (present if path.is_file() else pending).append((path, task, method, seed, identity))
            stages.append({"calibration_directory": str(directory),
                           **{key: stage_counts[key] for key in ("admitted", "rejected", "missing", "incomplete")}})
        admission_summary = {"configured_candidates": len(ids), "admitted_candidates": len(admitted_ids),
                             "rejected_candidates": len(rejected_ids),
                             "candidate_ids_with_both_admitted_and_rejected_stages": len(admitted_ids & rejected_ids),
                             "admission_records": {key: admissions[key] for key in ("admitted", "rejected", "missing", "incomplete")},
                             "admissions_by_calibration": stages, "rejected_trial_files_ignored": rejected_trial_files_ignored,
                             "pending_admission_records": pending_admissions,
                             "expected_admitted_trial_files": len(present) + len(pending),
                             "checkpoint_set_complete": not pending and not pending_admissions}
        if (pending or pending_admissions) and not allow_partial:
            outputs[topology] = {"status": "skipped_incomplete_checkpoint_set", **admission_summary,
                                 "completed_trial_files": len(present), "missing_admitted_trial_files": len(pending),
                                 "target_count": len(keys)}
            continue
        if not keys:
            outputs[topology] = {"status": "no_selected_targets", **admission_summary, "target_count": 0}
            continue
        vectors, invalid, valid, calls, physics, method_counts = {}, 0, 0, 0, set(), Counter()
        for path, task, method, seed, base_identity in present:
            physical_key = canonical_hash({key: task[key] for key in PHYSICAL_FIELDS})
            physics.add(physical_key)
            snapshot.read(path)
            trial = load_checkpoint(path, {**base_identity, "kind": "baseline", "method": method, "seed": seed})
            if trial is None:
                raise ValueError("Trial checkpoint is not marked complete.")
            validate_trial(trial, task, method, seed)  # Pure deterministic replay; no simulator callback.
            method_counts[method] += 1
            for row in trial["trajectory"]:
                calls += 1
                if row["status"] != "ok":
                    invalid += 1
                    continue
                valid += 1
                key = _hash(row["parameters"])
                measured = {field: row[field] for field in ("metrics", "independent_metrics")}
                if key in vectors:
                    if vectors[key]["measured"] != measured:
                        raise ValueError("The same physical parameter vector has conflicting saved measurements.")
                    vectors[key]["observations"] += 1
                    vectors[key]["original_hashes"].add(canonical_hash(row["parameters"]))
                else:
                    vectors[key] = {"measured": measured, "observations": 1,
                                    "original_hashes": {canonical_hash(row["parameters"])},
                                    "source_trial": str(path.relative_to(ROOT)), "evaluation": row["evaluation"],
                                    "measurement_key": row["measurement_key"], "measurement_directory": row["measurement_directory"]}
        if len(physics) > 1:
            raise ValueError("Cross-target reuse would mix different circuit/measurement conditions.")
        agreed, either, ambiguous, ambiguous_pass = {}, {}, 0, 0
        for vector_key, vector in vectors.items():
            mask, possible = 0, 0
            for bit, target in enumerate(chosen_targets):
                primary = score(vector["measured"]["metrics"], target["constraints"])
                native = score(vector["measured"]["independent_metrics"], target["constraints"])
                if primary["checks"] != native["checks"]:
                    ambiguous += 1
                if primary["success"] and native["success"]:
                    mask |= 1 << bit
                if primary["success"] or native["success"]:
                    possible |= 1 << bit
                if primary["success"] != native["success"]:
                    ambiguous_pass += 1
            agreed[vector_key], either[vector_key] = mask, possible
        accepted = covering(agreed, keys)
        upper = covering(either, keys)
        worst = accepted["worst_witness_key"]
        witness = vectors[worst] if worst is not None else None
        baseline_coverage = next(row["coverage"] for row in curation["topologies"] if row["topology"] == topology)
        order = baseline_coverage["target_keys_in_bit_order"]
        if set(keys) - set(order):
            raise ValueError("Frozen curation matrix does not cover the selected target keys.")
        positions = {key: bit for bit, key in enumerate(keys)}
        old_masks = {"construction:" + key: sum(1 << positions[target_key] for bit, target_key in enumerate(order)
                                                if target_key in positions and int(encoded, 16) & (1 << bit))
                     for key, encoded in baseline_coverage["coverage_bitmasks_hex"].items()}
        union = {**old_masks, **{"trajectory:" + key: mask for key, mask in agreed.items()}}
        outputs[topology] = {"status": "diagnostic_complete", **admission_summary,
            "trial_files_read": len(present), "missing_trial_files": len(pending), "trials_per_method": dict(method_counts),
            "logical_trajectory_evaluations": calls, "valid_evaluation_rows": valid, "invalid_evaluation_rows_excluded": invalid,
            "unique_valid_parameter_vectors": len(vectors), "physical_task_identity": next(iter(physics), None),
            "target_keys_in_bit_order": keys,
            "trajectory_agreed_coverage_bitmasks_hex": {key: hex(mask) for key, mask in sorted(agreed.items())},
            "trajectory_either_path_coverage_bitmasks_hex": {key: hex(mask) for key, mask in sorted(either.items())},
            "trajectory_witness_original_hashes": {key: sorted(vector["original_hashes"])
                                                   for key, vector in sorted(vectors.items())},
            "trajectory_mask_semantics": {
                "witness_key": "SHA-256 of the full parameter vector with simulator-equivalent 15-significant-digit numeric normalization",
                "agreed": "Both saved extraction paths meet every requirement for the target bit.",
                "either_path": "Conservative potential overlap: at least one saved path passes; this is not confirmed feasibility or a bound on unobserved physical truth.",
                "original_hashes": "Observed raw-JSON canonical_hash aliases for each normalized vector, including zero-coverage vectors; bridges matching original construction-pool witness keys.",
                "zero_masks_retained": True},
            "new_trajectory_witnesses_agreed_paths": accepted, "either_path_diagnostic_upper": upper,
            "cross_target_pairs_with_different_per_metric_checks": ambiguous,
            "cross_target_pairs_with_different_success_decisions": ambiguous_pass,
            "worst_witness_evidence": {key: value for key, value in witness.items()
                                       if key not in {"measured", "original_hashes"}} if witness else None,
            "frozen_construction_coverage": covering(old_masks, keys),
            "union_with_frozen_construction_coverage": covering(union, keys),
            "scope": "Only status-ok vectors in the immutable completed trial files listed in the input manifest. "
                     "Each trial was checkpoint-validated and deterministically replayed without simulation. "
                     "Every valid vector is rescored against every selected frozen target using both saved extraction paths. "
                     "Validated rejected admissions expect no baseline trials and contribute no trajectory witnesses. "
                     "The union additionally uses the frozen candidate curation matrix, with its original measurement/rounding limits; its witness count counts evidence masks and may duplicate physical vectors across pools. "
                     "Either-path coverage is conservative potential overlap, not confirmed feasibility. "
                     "Greedy cover is not an optimal cover or a lower bound; uncovered targets remain explicit. "
                     "This nonuniform, outcome-dependent screening pool establishes no solve probability, difficulty label, release approval, or absence of other widely covering solutions."}
    snapshot.assert_unchanged()
    return {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(), "status": "read_only_quality_diagnostic",
            "candidate_index": str(candidates / "index.json"), "targets_file": str(candidates / "targets.json"),
            "curation_report": str(candidates / "curation_report.json"),
            "topologies": outputs, "inputs_sha256": snapshot.hashes, "simulations_performed": 0,
            "cache_writes": 0, "training_approved": False, "release_qualification": "not_performed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, action="append", required=True)
    parser.add_argument("--names", nargs="+", required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Diagnostic output must be new.")
    result = run(args.candidates, args.calibration, args.names, selection=args.selection, allow_partial=args.allow_partial)
    save_json(args.output, result)
    for topology, row in result["topologies"].items():
        short = {key: row[key] for key in ("status", "configured_candidates") if key in row}
        if row["status"] == "diagnostic_complete":
            measured = row["new_trajectory_witnesses_agreed_paths"]
            union = row["union_with_frozen_construction_coverage"]
            short.update(unique_valid_vectors=row["unique_valid_parameter_vectors"],
                         maximum_coverage=measured["maximum_covered_targets"], targets=measured["target_count"],
                         greedy_cover=measured["greedy_cover_count"], observed_targets_solved=measured["covered_targets"],
                         union_maximum_coverage=union["maximum_covered_targets"], union_greedy_cover=union["greedy_cover_count"])
        print(json.dumps({"topology": topology, **short}), flush=True)


if __name__ == "__main__":
    main()
