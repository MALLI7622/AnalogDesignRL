"""Plan confirmation work from one-seed screening, without assigning difficulty.

Queues are scheduling priorities only. Each selected task still needs the full
declared confirmation seeds and the independent release gates. Reference values
are used only to validate admission, audit changed controls, and deduplicate.
No simulator/cache writes, training actions, or qualification changes occur.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path

from analog_design.model_clients import strict_json
from analog_design.simulator import ROOT, digest, parameters_for
from benchmark.baselines import METHODS
from benchmark.calibrate import (BUDGET, CODE_FILES, PHYSICAL_FIELDS, InputGuard,
                                 load_checkpoint, summarize_trials)
from benchmark.candidates import distance, requirement_identity
from benchmark.explore import rounded_parameters
from benchmark.frontier_select import admission_failures, start_changes
from benchmark.release import InputSnapshot, candidate_records, validate_evaluation, validate_trial
from benchmark.simulation import canonical_hash


QUEUES = ("fast", "medium_priority", "hard_priority")
DEFAULT_COUNTS = dict(zip(QUEUES, (120, 175, 40)))


def _screen_outcomes(methods):
    if set(methods) != set(METHODS) or len(METHODS) != 4:
        raise ValueError("Screening requires exactly the four declared methods.")
    outcomes = {}
    for method in METHODS:
        stats = methods[method]
        if type(stats.get("completed_trials")) is not int or stats["completed_trials"] != 1:
            raise ValueError("Each screening method must have exactly one completed trial.")
        success = stats.get("successes")
        calls = stats.get("median_calls_successes")
        used = stats.get("logical_evaluations")
        if (type(success) is not int or success not in {0, 1} or stats.get("failures") != 1 - success
                or type(used) is not int or not 1 <= used <= BUDGET
                or success and (type(calls) is not int or not 2 <= calls <= BUDGET or used != calls)
                or not success and calls is not None):
            raise ValueError("Invalid one-trial screening outcome.")
        if stats.get("median_calls_censored_at_30") != (calls if success else BUDGET):
            raise ValueError("Screening censored calls disagree with its single trial.")
        for cap in (5, 10, 30):
            row = stats["success_at"][str(cap)]
            expected = int(bool(success and calls <= cap))
            if row.get("trials") != 1 or row.get("successes") != expected or row.get("rate") != expected:
                raise ValueError("Screening success-at statistics disagree with its single trial.")
        outcomes[method] = {"success": bool(success), "first_success_evaluation": calls,
                            "evaluations_used": used}
    return outcomes


def _queue_priorities(outcomes, candidate, changes):
    calls = [row["first_success_evaluation"] for row in outcomes.values() if row["success"]]
    fast = [call for call in calls if call <= 10]
    slow = [call for call in calls if call > 10]
    priorities = {}
    if fast:
        priorities["fast"] = (0, min(fast), -len(fast))
    if changes["substantive_changed_control_count"] >= 2 and calls:
        priorities["medium_priority"] = (int(bool(fast)), -len(slow), len(fast), min(calls))
    if candidate.get("start_variant") == "distant" and changes["substantive_changed_control_count"] >= 3:
        priorities["hard_priority"] = (int(bool(calls)), len(calls), -min(calls, default=BUDGET + 1))
    return priorities


def plan(observations, counts=None, *, minimum_distance=0.025, per_topology_limit=None):
    """Reserve groups greedily, then try bounded direct reserve reassignments.

    Medium first prefers observed successes only at calls 11--30, then mixed or
    fast successes. All-failure intermediate starts remain manual reserves.
    Hard first prefers all four methods failing, with observed successes kept as
    explicit fallback prospects. A reassignment can free an occupied group for a
    missing queue and replace its displaced task from an unused group, without
    worsening the displaced queue's priority tier. Queue names are not labels.
    """
    counts = dict(DEFAULT_COUNTS if counts is None else counts)
    if set(counts) != set(QUEUES) or any(type(value) is not int or value < 0 for value in counts.values()) or not sum(counts.values()):
        raise ValueError("Provide nonnegative queue counts with a positive total.")
    if isinstance(minimum_distance, bool) or not math.isfinite(minimum_distance) or minimum_distance < 0.025:
        raise ValueError("The minimum reference distance must be at least 0.025.")
    if per_topology_limit is not None and (type(per_topology_limit) is not int or per_topology_limit < 1):
        raise ValueError("An optional topology limit must be a positive integer.")
    candidates, rejected, seen = [], [], set()
    for observation in observations:
        candidate, summary, admission = (observation[key] for key in ("candidate", "observation", "admission"))
        name, task = candidate["id"], candidate["task"]
        if name in seen:
            raise ValueError("A screening task must appear in exactly one input run.")
        seen.add(name)
        if (task.get("id") != name or summary.get("id") != name or admission.get("id") != name
                or admission.get("effective_task") != task or admission.get("rounded_reference_parameters") != candidate["reference"]):
            raise ValueError("Screening task or admission identity disagrees with the candidate.")
        group = canonical_hash(requirement_identity(task))
        if candidate.get("requirement_group") != group:
            raise ValueError("Candidate requirement group disagrees with its task.")
        failures, reason = admission_failures(candidate, admission)
        if reason:
            rejected.append({"id": name, "reason": reason})
            continue
        if summary.get("admitted") is not True:
            raise ValueError("Screening summary and admission disagree.")
        outcomes = _screen_outcomes(summary["methods"])
        changes = start_changes(candidate)
        priorities = _queue_priorities(outcomes, candidate, changes)
        candidates.append({"candidate": candidate, "priorities": priorities, "outcomes": outcomes,
            "changes": changes, "failures": failures, "calibration_directory": observation["calibration_directory"],
            "screening_seed": observation["screening_seed"], "group": group,
            "reference_key": canonical_hash(candidate["reference"])})

    selected, selected_ids, groups, references = [], set(), set(), defaultdict(list)
    total_topologies, queue_topologies, selected_counts = Counter(), defaultdict(Counter), Counter()
    by_id = {row["candidate"]["id"]: row for row in candidates}
    by_group = defaultdict(list)
    for row in candidates:
        by_group[row["group"]].append(row)

    def conflict(row, state=None):
        reserved_ids, reserved_groups, reserved_references, topology_counts = (
            (selected_ids, groups, references, total_topologies) if state is None else state)
        candidate, topology = row["candidate"], row["candidate"]["topology"]
        if candidate["id"] in reserved_ids:
            return "already_selected"
        if row["group"] in reserved_groups:
            return "requirement_group_reserved"
        if per_topology_limit is not None and topology_counts[topology] >= per_topology_limit:
            return "topology_limit"
        for key, values in reserved_references[topology]:
            if row["reference_key"] == key:
                return "reference_reserved"
            if distance(candidate["task"], candidate["reference"], values) < minimum_distance:
                return "reference_too_close"
        return None

    def reserve(row, state):
        ids, reserved_groups, vectors, topology_counts = state
        candidate, topology = row["candidate"], row["candidate"]["topology"]
        ids.add(candidate["id"])
        reserved_groups.add(row["group"])
        vectors[topology].append((row["reference_key"], candidate["reference"]))
        topology_counts[topology] += 1

    def selection_state(rows, *, verify=False):
        state = (set(), set(), defaultdict(list), Counter())
        for selected_row in rows:
            row = by_id[selected_row["id"]]
            queue = selected_row["queue"]
            if (queue not in row["priorities"] or selected_row["priority_tier"] != row["priorities"][queue][0]
                    or verify and conflict(row, state)):
                return None
            reserve(row, state)
        return state

    def public_row(row, queue=None):
        candidate = row["candidate"]
        result = {"id": candidate["id"], "topology": candidate["topology"],
            "topology_family": candidate["topology_family"], "start_variant": candidate.get("start_variant"),
            "requirement_group": row["group"], "reference_key": row["reference_key"],
            "substantive_changed_control_count": row["changes"]["substantive_changed_control_count"],
            "calibration_directory": str(row["calibration_directory"]), "screening_seed": row["screening_seed"],
            "screening_outcomes": deepcopy(row["outcomes"]), "initial_clear_failure_metrics": row["failures"],
            "eligible_queues": list(row["priorities"]),
            "queue_priority_tiers": {name: priority[0] for name, priority in row["priorities"].items()},
            "difficulty_status": "unassigned"}
        if queue is not None:
            result.update(queue=queue, priority_tier=row["priorities"][queue][0])
        return result

    for queue in ("hard_priority", "medium_priority", "fast"):
        while selected_counts[queue] < counts[queue]:
            available = [row for row in candidates if queue in row["priorities"] and conflict(row) is None]
            if not available:
                break
            chosen = min(available, key=lambda row: (
                row["priorities"][queue][0], total_topologies[row["candidate"]["topology"]],
                queue_topologies[queue][row["candidate"]["topology"]],
                row["priorities"][queue][1:], row["candidate"]["id"]))
            candidate, topology = chosen["candidate"], chosen["candidate"]["topology"]
            selected.append(public_row(chosen, queue))
            selected_ids.add(candidate["id"])
            groups.add(chosen["group"])
            references[topology].append((chosen["reference_key"], candidate["reference"]))
            total_topologies[topology] += 1
            queue_topologies[queue][topology] += 1
            selected_counts[queue] += 1

    greedy_counts = {queue: selected_counts[queue] for queue in QUEUES}
    maximum_repairs = sum(counts[queue] - selected_counts[queue] for queue in QUEUES)
    repairs = []
    # Each accepted direct reassignment fills exactly one missing slot. A missing
    # direct move leaves an explicit partial plan; no unbounded graph search.
    for _ in range(maximum_repairs):
        options = []
        for queue in ("hard_priority", "medium_priority", "fast"):
            if selected_counts[queue] >= counts[queue]:
                continue
            for removed in selected:
                displaced_queue = removed["queue"]
                if displaced_queue == queue:
                    continue
                alternatives = [row for row in by_group[removed["requirement_group"]] if queue in row["priorities"]]
                if not alternatives:
                    continue
                replacements = [row for row in candidates if row["group"] not in groups
                                and displaced_queue in row["priorities"]
                                and row["priorities"][displaced_queue][0] <= removed["priority_tier"]]
                for alternative in alternatives:
                    for replacement in replacements:
                        topology = replacement["candidate"]["topology"]
                        priority = (topology != removed["topology"],
                            alternative["priorities"][queue][0], replacement["priorities"][displaced_queue][0],
                            total_topologies[topology], queue_topologies[queue][alternative["candidate"]["topology"]],
                            replacement["priorities"][displaced_queue][1:], alternative["priorities"][queue][1:],
                            queue, displaced_queue, replacement["candidate"]["id"], alternative["candidate"]["id"], removed["id"])
                        options.append((priority, queue, removed, alternative, replacement))
        accepted = None
        for _, queue, removed, alternative, replacement in sorted(options, key=lambda row: row[0]):
            remaining = [row for row in selected if row["id"] != removed["id"]]
            state = selection_state(remaining)
            if conflict(alternative, state):
                continue
            reserve(alternative, state)
            if conflict(replacement, state):
                continue
            trial = remaining + [public_row(alternative, queue), public_row(replacement, removed["queue"])]
            checked = selection_state(trial, verify=True)
            if checked is None:
                raise RuntimeError("Combined reserve reassignment failed its full identity, distance or topology audit.")
            accepted = trial, checked, queue, removed, alternative, replacement
            break
        if accepted is None:
            break
        selected, state, queue, removed, alternative, replacement = accepted
        selected_ids, groups, references, total_topologies = state
        selected_counts = Counter(row["queue"] for row in selected)
        queue_topologies = defaultdict(Counter)
        for row in selected:
            queue_topologies[row["queue"]][row["topology"]] += 1
        repairs.append({"filled_queue": queue, "displaced_queue": removed["queue"],
            "removed_task_id": removed["id"], "reassigned_task_id": alternative["candidate"]["id"],
            "replacement_task_id": replacement["candidate"]["id"],
            "reassigned_requirement_group": removed["requirement_group"],
            "replacement_requirement_group": replacement["group"],
            "displaced_priority_tier": removed["priority_tier"],
            "replacement_priority_tier": replacement["priorities"][removed["queue"]][0],
            "reassigned_priority_tier": alternative["priorities"][queue][0],
            "same_topology_replacement": replacement["candidate"]["topology"] == removed["topology"],
            "combined_identity_distance_topology_checks_passed": True})

    reserves = [{**public_row(row), "reservation_conflict": conflict(row),
                 "manual_review_reason": None if row["priorities"] else "No observed success for fast/medium; hard curation not met."}
                for row in sorted(candidates, key=lambda item: item["candidate"]["id"])
                if row["candidate"]["id"] not in selected_ids]
    shortfalls = {queue: counts[queue] - selected_counts[queue] for queue in QUEUES}
    report = {"requested_queues": counts, "selected_queues": {queue: selected_counts[queue] for queue in QUEUES},
        "shortfalls": shortfalls, "complete": not any(shortfalls.values()),
        "screened_task_count": len(observations), "admission_rejections": rejected,
        "admitted_task_count": len(candidates), "selected_unique_requirement_groups": len(groups),
        "selected_unique_references": sum(len(rows) for rows in references.values()),
        "selected_topologies": dict(sorted(total_topologies.items())),
        "selected_queue_topologies": {queue: dict(sorted(queue_topologies[queue].items())) for queue in QUEUES},
        "available_queues": {queue: sum(queue in row["priorities"] for row in candidates) for queue in QUEUES},
        "available_unique_groups": {queue: len({row["group"] for row in candidates if queue in row["priorities"]}) for queue in QUEUES},
        "fallback_selected": {queue: sum(row["queue"] == queue and row["priority_tier"] > 0 for row in selected) for queue in QUEUES},
        "greedy_selected_queues": greedy_counts, "reserve_reassignments": repairs,
        "reserve_reassignment_policy": {
            "maximum_steps": maximum_repairs,
            "scope": "Bounded direct moves only, using existing eligible candidates; no final difficulty assignment.",
            "ranking": "Same-topology replacement first, then priority tiers, topology balance, existing queue priorities and stable IDs.",
            "tier_rule": "The replacement preserves or improves the displaced queue member's priority tier.",
            "gates": "Recheck all selected IDs, groups, references, distances and optional topology caps after each combined move."},
        "manual_reserve_count": len(reserves), "minimum_reference_distance": minimum_distance,
        "per_topology_limit": per_topology_limit,
        "scope": "Provisional scheduling from one screening seed; no final difficulty labels, feasibility claim, or confirmation counts.",
        "ranking_inputs": "Observed method successes/calls and topology balance. Reference values only validate admission, changes, uniqueness and distance.",
        "shortfall_note": "A shortfall after greedy selection and bounded direct reassignments is not proof that no compatible allocation exists; inspect reserves before adding work."}
    return {"schema_version": 1,
        "status": "provisional_confirmation_plan_complete" if report["complete"] else "provisional_confirmation_plan_partial",
        "ids": [row["id"] for row in selected], "tasks": selected, "reserves": reserves, "report": report,
        "reserve_ids_by_queue": {queue: [row["candidate"]["id"] for row in sorted(candidates,
            key=lambda item: (item["priorities"].get(queue, (99,)), item["candidate"]["id"]))
            if row["candidate"]["id"] not in selected_ids and queue in row["priorities"]] for queue in QUEUES},
        "difficulty_status": "unassigned", "confirmation_status": "not_run", "training_approved": False,
        "confirmation_policy": "Use new disjoint confirmation seeds for all four methods; at least 10 per method for easy/medium and 20 for hard. Do not pool or relabel screening trials."}


def plan_files(candidate_directory, calibration_directories, output, counts=None, **options):
    """Validate complete one-seed checkpoint records before planning any IDs.

    Trial actions/scores are replayed from their observations without simulation.
    This does not replace final assembly's independent physical-cache audit.
    Repeated calibration arguments must cover disjoint task IDs; no seed pooling
    or silent choice among repeated observations is performed.
    """
    directory, output = Path(candidate_directory).resolve(), Path(output).resolve()
    calibrations = [Path(path).resolve() for path in calibration_directories]
    if output.exists():
        raise FileExistsError("Confirmation plan output must be new.")
    if not calibrations or len(set(calibrations)) != len(calibrations):
        raise ValueError("Provide distinct complete screening directories.")
    catalog_path = ROOT / "benchmark/domains.json"
    initial_guard = InputGuard([directory / "index.json", catalog_path, Path(__file__),
                               *[path / name for path in calibrations for name in ("manifest.json", "summary.json")]])
    snapshot = InputSnapshot()
    indexed = candidate_records(directory, snapshot)
    domains = {row["name"]: row for row in strict_json(catalog_path.read_text())["entries"]}
    sources, paths = [], [Path(path) for path in initial_guard.hashes]
    paths += [catalog_path, *[entry["absolute_path"] for entry in indexed.values()]]
    paths += [ROOT / path for path in (*CODE_FILES, "benchmark/release.py", "benchmark/frontier_select.py")]
    for calibration in calibrations:
        manifest = strict_json((calibration / "manifest.json").read_text())
        summary = strict_json((calibration / "summary.json").read_text())
        config = manifest["configuration"]
        config_hash = canonical_hash(config)
        methods, seeds, names = config["methods"], config["seeds"], config["candidate_ids"]
        if (manifest.get("schema_version") != 1 or manifest.get("configuration_sha256") != config_hash
                or set(methods) != set(METHODS) or len(methods) != 4
                or not isinstance(seeds, list) or len(seeds) != 1 or type(seeds[0]) is not int
                or len(names) != len(set(names)) or set(names) - set(indexed)
                or config.get("budget") != BUDGET or config.get("simulator_timeout_s") != 30):
            raise ValueError("Screening manifest must bind complete one-seed, four-method inputs.")
        if (summary.get("status") != "complete" or summary.get("methods") != methods or summary.get("seeds") != seeds
                or summary.get("budget") != BUDGET or summary.get("task_count") != len(names)
                or summary.get("admission_count") != len(names) or summary.get("remaining_trials") != 0):
            raise ValueError("Screening summary is incomplete or differs from its manifest.")
        rows = {row["id"]: row for row in summary["tasks"]}
        if len(rows) != len(summary["tasks"]) or set(rows) != set(names):
            raise ValueError("Screening summary task IDs disagree with its manifest.")
        required = [directory / "index.json", catalog_path, *[ROOT / path for path in CODE_FILES],
                    *[indexed[name]["absolute_path"] for name in names]]
        for path in required:
            if config["inputs_sha256"].get(str(path.resolve())) != digest(path):
                raise ValueError("Screening frozen input does not match the candidate/domain/code: " + str(path))
        for filename, expected in config["inputs_sha256"].items():
            if digest(filename) != expected:
                raise ValueError("Screening input changed: " + filename)
            paths.append(Path(filename))
        physics = manifest["physical_identities"]
        if config["physics"] != {key: canonical_hash(value) for key, value in physics.items()}:
            raise ValueError("Screening physical identity hashes disagree.")
        for name in names:
            paths.append(calibration / name / "admission.json")
            paths += [calibration / name / f"{method}_{seeds[0]}.json" for method in methods
                      if (calibration / name / f"{method}_{seeds[0]}.json").is_file()]
        sources.append((calibration, config, config_hash, physics, summary, rows))
    guard = InputGuard(paths)
    initial_guard.assert_unchanged()
    for _, config, *_ in sources:
        if any(guard.hashes[str(Path(filename).resolve())] != expected
               for filename, expected in config["inputs_sha256"].items()):
            raise ValueError("A declared screening input changed before the planning snapshot.")
    observations, seen = [], set()
    for calibration, config, config_hash, physics, summary, rows in sources:
        actual_trials, admitted_count = 0, 0
        for name, row in rows.items():
            if name in seen:
                raise ValueError("A task appears in multiple screening runs; choose one explicitly.")
            seen.add(name)
            candidate_path = indexed[name]["absolute_path"]
            candidate = strict_json(candidate_path.read_text())
            task = candidate["task"]
            domain = domains[candidate["topology"]]
            if (candidate.get("id") != name or task.get("id") != name
                    or any(candidate.get(key) != indexed[name].get(key) for key in ("topology", "topology_family"))
                    or any(candidate.get(key) != row.get(key) for key in ("topology", "topology_family", "proposed_difficulty", "profile"))
                    or any(task[key] != domain["task"][key] for key in PHYSICAL_FIELDS)
                    or candidate["topology_family"] != domain["family"]):
                raise ValueError("Screening candidate task/source identity disagrees.")
            if (set(candidate["reference"]) != set(task["parameters"])
                    or rounded_parameters(task, task["initial_parameters"]) != task["initial_parameters"]
                    or rounded_parameters(task, candidate["reference"]) != candidate["reference"]):
                raise ValueError("Screening task/reference differs from its rounded full vector.")
            parameters_for(task, candidate["reference"])
            physical_key = canonical_hash({key: task[key] for key in PHYSICAL_FIELDS})
            physical = physics[physical_key]
            if any(physical[key] != task[key] for key in PHYSICAL_FIELDS):
                raise ValueError("Screening physical identity differs from its task.")
            identity = {"configuration_sha256": config_hash, "candidate_sha256": digest(candidate_path),
                        "task_sha256": canonical_hash(task), "reference_sha256": canonical_hash(candidate["reference"]),
                        "public_default_sha256": canonical_hash(domain["public_default"]),
                        "physics_sha256": canonical_hash(physical)}
            admission = load_checkpoint(calibration / name / "admission.json", {**identity, "kind": "admission"})
            if (admission is None or admission.get("id") != name or admission.get("topology") != candidate["topology"]
                    or type(admission.get("admitted")) is not bool or admission.get("logical_evaluations") != 3
                    or set(admission.get("evaluations", {})) != {"initial", "reference", "public_seed"}
                    or admission.get("effective_task") != task or admission.get("rounded_reference_parameters") != candidate["reference"]
                    or admission.get("public_default_parameters") != domain["public_default"]
                    or row.get("admitted") is not admission.get("admitted")):
                raise ValueError("Screening admission task, reference, or summary mismatch.")
            for measured in admission["evaluations"].values():
                validate_evaluation(measured, task)
            trial_records, reconstructed = [], {}
            for method in METHODS:
                path = calibration / name / f"{method}_{config['seeds'][0]}.json"
                trial = load_checkpoint(path, {**identity, "kind": "baseline", "method": method, "seed": config["seeds"][0]})
                if admission["admitted"]:
                    if trial is None:
                        raise ValueError("A completed screening task is missing a method trial.")
                    validate_trial(trial, task, method, config["seeds"][0])
                    first, initial = trial["trajectory"][0], admission["evaluations"]["initial"]
                    if any(first.get(key) != initial.get(key) for key in ("status", "success", "metrics", "checks", "reward")):
                        raise ValueError("Screening initial observation differs from admission.")
                    reconstructed[method] = summarize_trials([trial])
                    trial_records.append(trial)
                else:
                    if trial is not None:
                        raise ValueError("Rejected screening tasks must not have baseline trials.")
                    reconstructed[method] = summarize_trials([])
            if (row.get("methods") != reconstructed or row.get("pooled") != summarize_trials(trial_records)
                    or row.get("expected_trials") != len(trial_records)):
                raise ValueError("Screening summary counts disagree with complete trial checkpoints.")
            actual_trials += len(trial_records)
            admitted_count += admission["admitted"] is True
            observations.append({"candidate": candidate, "observation": row, "admission": admission,
                                 "calibration_directory": str(calibration), "screening_seed": config["seeds"][0]})
        if (summary.get("completed_trials") != actual_trials or summary.get("expected_trials") != actual_trials
                or summary.get("admitted_count") != admitted_count or summary.get("rejected_count") != len(rows) - admitted_count):
            raise ValueError("Screening aggregate completion counts disagree with checkpoints.")
    result = plan(observations, counts, **options)
    guard.assert_unchanged()
    result["inputs_sha256"] = guard.hashes
    result["evidence_scope"] = "Frozen input and checkpoint identities, full trial action/score replay, and recomputed admission checks; final physical-cache and fresh-simulation release verification remain separate."
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--counts", nargs=3, type=int, default=[120, 175, 40], metavar=("FAST", "MEDIUM_PRIORITY", "HARD_PRIORITY"))
    parser.add_argument("--minimum-distance", type=float, default=0.025)
    parser.add_argument("--per-topology-limit", type=int)
    args = parser.parse_args()
    result = plan_files(args.candidates, args.calibration, args.output, dict(zip(QUEUES, args.counts)),
                        minimum_distance=args.minimum_distance, per_topology_limit=args.per_topology_limit)
    print(json.dumps({"status": result["status"], **result["report"]}, indent=2))


if __name__ == "__main__":
    main()
