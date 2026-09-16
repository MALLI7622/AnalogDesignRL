"""Select confirmed sizing tasks while auditing measured solution overlap.

Difficulty comes only from select.observed_level. Coverage is computed over the
actual selected target subset using every witness in the prepared measured pool;
it is neither a solve probability nor a proof of minimum set-cover size. This
offline selector does not replace fresh release verification or training review.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path
import re

from analog_design.metrics import score
from benchmark import select as selection_policy
from benchmark.calibrate import InputGuard, load_checkpoint
from benchmark.candidates import distance, reference_margin, requirement_identity
from benchmark.select import load_observations, observed_level
from benchmark.simulation import canonical_hash, save_json
from benchmark.start_designs import clear_failures


LEVELS = ("easy", "medium", "hard")
SUBSTANTIVE_CONTINUOUS_CHANGE = 0.025


class PreparedPool:
    """Index complete prepared support sets without restricting witnesses to refs."""

    def __init__(self, targets, curation_report=None):
        if not isinstance(targets, list) or not targets:
            raise ValueError("The complete prepared target list is required.")
        self.targets, self.support, self.masks, self.positions = {}, {}, {}, {}
        self.original_witness_keys = None
        self.trajectory_evidence = []
        for target in targets:
            topology, key = target["topology"], target["key"]
            identity = (topology, key)
            if identity in self.targets or key != canonical_hash(target["parameters"]):
                raise ValueError("Duplicate or inconsistent prepared reference identity.")
            support = target.get("empirical_feasible_design_keys")
            if (not isinstance(support, list) or not support or len(set(support)) != len(support)
                    or not all(isinstance(value, str) and value for value in support)):
                raise ValueError("Full prepared support keys are required; numeric legacy indices are unsupported.")
            if target.get("empirical_solution_signature") != canonical_hash(sorted(support)):
                raise ValueError("Prepared support signature disagrees with its design keys.")
            if target.get("empirical_pool_feasible_count") != len(support):
                raise ValueError("Prepared feasible-count metadata disagrees with its support.")
            if key not in support:
                raise ValueError("The prepared reference must belong to its own measured support.")
            position = len(self.positions.setdefault(topology, {}))
            self.positions[topology][key] = position
            self.targets[identity] = target
            self.support[identity] = frozenset(support)
            masks = self.masks.setdefault(topology, {})
            for witness in support:
                masks[witness] = masks.get(witness, 0) | (1 << position)
        self.evidence = "full_prepared_support_keys"
        self.pool_counts = {name: max(target["empirical_pool_valid_count"] for (topology, _), target in self.targets.items()
                                     if topology == name) for name in self.positions}
        for topology, count in self.pool_counts.items():
            if (type(count) is not int or count < len(self.masks[topology]) or
                    any(target["empirical_pool_valid_count"] != count for (name, _), target in self.targets.items() if name == topology)):
                raise ValueError("Prepared targets do not describe one consistent measured pool per topology.")
        if curation_report is not None:
            reports = {row["topology"]: row["coverage"] for row in curation_report["topologies"]}
            if set(reports) != set(self.positions):
                raise ValueError("Curation coverage does not cover exactly the prepared topologies.")
            self.original_witness_keys = {}
            for topology, positions in self.positions.items():
                coverage = reports[topology]
                order = coverage["target_keys_in_bit_order"]
                if len(order) != len(positions) or set(order) != set(positions):
                    raise ValueError("Curation bit order does not cover the complete prepared target pool.")
                reconstructed = {}
                for witness, encoded in coverage["coverage_bitmasks_hex"].items():
                    mask = int(encoded, 16)
                    if mask < 0 or mask >> len(order):
                        raise ValueError("Curation coverage bitmask contains unknown target bits.")
                    translated = sum(1 << positions[key] for i, key in enumerate(order) if mask & (1 << i))
                    if translated:
                        reconstructed[witness] = translated
                if reconstructed != self.masks[topology]:
                    raise ValueError("Prepared support sets disagree with the full-pool coverage bitmasks.")
                if len(coverage["coverage_bitmasks_hex"]) != coverage["characterized_design_count"]:
                    raise ValueError("Curation measured-pool count disagrees with coverage witnesses.")
                if coverage["characterized_design_count"] != self.pool_counts[topology]:
                    raise ValueError("Prepared and curation measured-pool counts disagree.")
                # Zero masks matter when counting the union with later witnesses.
                self.original_witness_keys[topology] = frozenset(coverage["coverage_bitmasks_hex"])
                self.masks[topology].update({key: 0 for key in self.original_witness_keys[topology]
                                             if key not in self.masks[topology]})
            self.evidence = "full_prepared_support_keys_crosschecked_against_curation_bitmasks"
        self.original_pool_counts = dict(self.pool_counts)
        self.coverage_support = dict(self.support)

    def add_trajectory_coverage(self, reports, required_targets):
        """OR complete, validated diagnostic masks into conservative overlap evidence.

        Original support sets and signatures retain their construction meaning.
        Alias matching uses only exact serialized parameter hashes reported by the
        diagnostic; no approximate or construction-grid rounding is performed.
        Input-file hash validation is the responsibility of select_files.
        """
        if self.original_witness_keys is None:
            raise ValueError("Trajectory coverage requires the full curation report, including zero-mask witness keys.")
        if not reports:
            raise ValueError("At least one trajectory coverage report is required.")
        seen_topologies, aliases_by_topology, rows = set(), defaultdict(dict), []
        for report in reports:
            if report.get("schema_version") != 1 or not isinstance(report.get("topologies"), dict) or not report["topologies"]:
                raise ValueError("Invalid trajectory coverage report schema.")
            for topology, row in report["topologies"].items():
                if topology not in self.positions:
                    raise ValueError("Trajectory report names an unknown prepared topology.")
                if (row.get("status") != "diagnostic_complete" or row.get("checkpoint_set_complete") is not True
                        or row.get("pending_admission_records", 0) != 0 or row.get("missing_trial_files", 0) != 0):
                    raise ValueError("Trajectory coverage requires a complete diagnostic checkpoint set.")
                positions = self.positions[topology]
                order = row.get("target_keys_in_bit_order")
                if (not isinstance(order, list) or not order or not all(isinstance(key, str) for key in order)
                        or len(set(order)) != len(order) or set(order) - set(positions)):
                    raise ValueError("Trajectory target bit order must contain unique known prepared target keys.")
                if set(required_targets.get(topology, ())) - set(order):
                    raise ValueError("Trajectory diagnostic omits candidate targets for a used topology.")
                agreed = row.get("trajectory_agreed_coverage_bitmasks_hex")
                either = row.get("trajectory_either_path_coverage_bitmasks_hex")
                aliases = row.get("trajectory_witness_original_hashes")
                if (not isinstance(agreed, dict) or not isinstance(either, dict) or not isinstance(aliases, dict)
                        or set(agreed) != set(either) or set(aliases) != set(either)):
                    raise ValueError("Trajectory agreed/either-path masks and exact alias maps need the same complete witness keys.")
                if row.get("unique_valid_parameter_vectors", len(either)) != len(either):
                    raise ValueError("Trajectory witness count disagrees with its complete mask maps.")
                translated = {}
                for key in either:
                    if not _sha256(key):
                        raise ValueError("Trajectory witness keys must be parameter SHA-256 hashes.")
                    first, second = (_coverage_mask(mapping[key], len(order)) for mapping in (agreed, either))
                    if first & ~second:
                        raise ValueError("Trajectory agreed coverage must be a subset of either-path coverage.")
                    originals = aliases[key]
                    if (not isinstance(originals, list) or not originals or not all(_sha256(value) for value in originals)
                            or len(set(originals)) != len(originals)):
                        raise ValueError("Trajectory aliases must be distinct exact serialized parameter hashes.")
                    for original in originals:
                        previous = aliases_by_topology[topology].setdefault(original, key)
                        if previous != key:
                            raise ValueError("One exact trajectory alias cannot identify different normalized witnesses.")
                    translated[key] = sum(1 << positions[target] for i, target in enumerate(order) if second & (1 << i))
                rows.append((topology, translated))
                seen_topologies.add(topology)
        if set(required_targets) - seen_topologies:
            raise ValueError("Trajectory diagnostics are missing a used candidate topology.")
        normalized_by_topology = defaultdict(set)
        for topology, translated in rows:
            normalized_by_topology[topology].update(translated)
        for topology, aliases in aliases_by_topology.items():
            if any(original in normalized_by_topology[topology] and original != normalized
                   for original, normalized in aliases.items()):
                raise ValueError("Trajectory aliases conflict with a distinct normalized witness identity.")
        # Validate every report before mutating the effective pool.
        for topology, aliases in aliases_by_topology.items():
            masks = self.masks[topology]
            for original, normalized in aliases.items():
                if original != normalized and original in masks:
                    masks[normalized] = masks.get(normalized, 0) | masks.pop(original)
        for topology, translated in rows:
            masks = self.masks[topology]
            for key, mask in translated.items():
                masks[key] = masks.get(key, 0) | mask
        effective = {identity: set() for identity in self.targets}
        for topology, positions in self.positions.items():
            by_position = {position: key for key, position in positions.items()}
            for witness, mask in self.masks[topology].items():
                while mask:
                    bit = mask & -mask
                    effective[(topology, by_position[bit.bit_length() - 1])].add(witness)
                    mask ^= bit
            if topology in seen_topologies:
                self.pool_counts[topology] = len(self.masks[topology])
        self.coverage_support = {identity: frozenset(support) for identity, support in effective.items()}
        self.evidence = "prepared_construction_plus_trajectory_either_path_potential_coverage"
        self.trajectory_evidence = [{"topology": topology, "witness_count_in_report": len(masks)} for topology, masks in rows]

    def target_for(self, candidate):
        identity = (candidate["topology"], candidate.get("target_key", canonical_hash(candidate["reference"])))
        target = self.targets.get(identity)
        if target is None:
            raise ValueError("Candidate is absent from the prepared target pool.")
        group = canonical_hash(requirement_identity(candidate["task"]))
        if (candidate["reference"] != target["parameters"] or candidate["task"]["constraints"] != target["constraints"]
                or candidate.get("requirement_group") != group or target["requirement_group"] != group
                or candidate.get("empirical_solution_signature") != target["empirical_solution_signature"]):
            raise ValueError("Candidate task/reference identity differs from its prepared target.")
        return identity

    def coverage(self, topology, target_keys):
        target_keys = set(target_keys)
        positions = self.positions[topology]
        if target_keys - set(positions):
            raise ValueError("Unknown target in selected-subset coverage audit.")
        selected = sum(1 << positions[key] for key in target_keys)
        masks = [(key, mask & selected) for key, mask in self.masks[topology].items() if mask & selected]
        maximum = max((mask.bit_count() for _, mask in masks), default=0)
        remaining, greedy = selected, []
        while remaining:
            # Stable key tie-breaking; greedy count is an upper bound on optimum.
            key, mask = min(masks, key=lambda item: (-(item[1] & remaining).bit_count(), item[0]), default=(None, 0))
            added = (mask & remaining).bit_count()
            if not added:
                break
            greedy.append({"design_key": key, "newly_covered": added})
            remaining &= ~mask
        return {"selected_task_count": len(target_keys), "prepared_target_count": len(positions),
                "full_measured_pool_design_count": self.pool_counts[topology],
                "original_prepared_pool_design_count": self.original_pool_counts[topology],
                "max_single_design_coverage": maximum,
                "max_single_design_fraction": maximum / len(target_keys) if target_keys else None,
                "greedy_cover_count": len(greedy) if not remaining else None,
                "uncovered_task_count": remaining.bit_count(), "greedy_steps": greedy}


def _sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _coverage_mask(encoded, bits):
    if not isinstance(encoded, str) or re.fullmatch(r"0x[0-9a-fA-F]+", encoded) is None:
        raise ValueError("Trajectory coverage masks must be hexadecimal strings.")
    value = int(encoded, 16)
    if value >> bits:
        raise ValueError("Trajectory coverage mask contains unknown target bits.")
    return value


def admission_failures(candidate, admission):
    """Require exact calibrated inputs and a clear failed metric in both paths."""
    if (admission.get("admitted") is not True or admission.get("public_seed_status") != "valid_failure"
            or admission.get("effective_task") != candidate["task"]
            or admission.get("rounded_reference_parameters") != candidate["reference"]):
        return [], "missing_valid_exact_admission"
    constraints = candidate["task"]["constraints"]
    measured = admission.get("evaluations", {})
    for phase, expected in (("initial", False), ("reference", True), ("public_seed", False)):
        row = measured.get(phase, {})
        if row.get("status") != "ok" or row.get("success") is not expected:
            return [], "invalid_" + phase
        primary = score(row.get("metrics", {}), constraints)
        native = score(row.get("independent_metrics", {}), constraints)
        if primary["success"] is not expected or primary["checks"] != native["checks"]:
            return [], "inconsistent_" + phase + "_checks"
        if phase == "reference" and not all(reference_margin(row[field], constraints)
                                              for field in ("metrics", "independent_metrics")):
            return [], "reference_margin"
    initial = measured["initial"]
    failures = sorted(set(clear_failures(initial["metrics"], constraints)) &
                      set(clear_failures(initial["independent_metrics"], constraints)))
    return (failures, None) if failures else ([], "no_common_clear_initial_failure")


def start_changes(candidate):
    """Recompute private construction evidence; never infer difficulty from it."""
    task, reference = candidate["task"], candidate["reference"]
    initial, rules = task["initial_parameters"], task["parameters"]
    if set(initial) != set(reference) or set(initial) != set(rules):
        raise ValueError("Starting and reference vectors must contain every editable control.")
    changed, substantive = [], []
    for name, rule in rules.items():
        first, second, low, high = initial[name], reference[name], rule["min"], rule["max"]
        if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in (first, second, low, high)) or low > high or
                not low <= first <= high or not low <= second <= high):
            raise ValueError("Invalid bounds or values in private start-change audit.")
        if rule.get("integer") and (first != int(first) or second != int(second)):
            raise ValueError("Integer controls must contain integral start/reference values.")
        if first == second:
            continue
        changed.append(name)
        if rule.get("integer"):
            enough = abs(first - second) >= 1
        else:
            amount = (abs(math.log(first / second) / math.log(high / low)) if low > 0 else
                      abs(first - second) / (high - low))
            enough = amount >= SUBSTANTIVE_CONTINUOUS_CHANGE
        if enough:
            substantive.append(name)
    return {"changed_controls": sorted(changed), "substantive_changed_controls": sorted(substantive),
            "changed_control_count": len(changed), "substantive_changed_control_count": len(substantive)}


def start_curation(candidate, level):
    changes = start_changes(candidate)
    count = changes["substantive_changed_control_count"]
    reason = ("medium_fewer_than_two_substantive_controls" if level == "medium" and count < 2 else
              "hard_requires_distant_variant" if level == "hard" and candidate.get("start_variant") != "distant" else
              "hard_fewer_than_three_substantive_controls" if level == "hard" and count < 3 else None)
    return {**changes, "start_variant": candidate.get("start_variant"),
            "start_change_curation_passed": reason is None, "rejection_reason": reason}


def _compatible(candidate, selected, topology_limit, minimum_distance):
    topology = candidate["topology"]
    same = [row["candidate"] for row in selected if row["candidate"]["topology"] == topology]
    if len(same) >= topology_limit:
        return "topology_cap"
    if any(other["candidate"]["requirement_group"] == candidate["requirement_group"] for other in selected):
        return "duplicate_requirement_group"
    for other in same:
        if canonical_hash(other["reference"]) == canonical_hash(candidate["reference"]):
            return "duplicate_reference"
        if other["empirical_solution_signature"] == candidate["empirical_solution_signature"]:
            return "same_measured_solution_set"
        if distance(candidate["task"], candidate["reference"], other["reference"]) < minimum_distance:
            return "reference_too_close"
    return None


def audit_selection(selected, pool, *, maximum_coverage_fraction=0.60,
                    minimum_greedy_cover=4, substantive_tasks=10):
    grouped = defaultdict(list)
    for row in selected:
        grouped[row["candidate"]["topology"]].append(row["candidate"])
    topologies, violations = {}, []
    for topology, candidates in sorted(grouped.items()):
        coverage = pool.coverage(topology, [pool.target_for(candidate)[1] for candidate in candidates])
        coverage["requirement_group_count"] = len({row["requirement_group"] for row in candidates})
        reasons = []
        if coverage["max_single_design_fraction"] > maximum_coverage_fraction:
            reasons.append("single_design_coverage_exceeds_cap")
        if coverage["uncovered_task_count"]:
            reasons.append("uncovered_selected_tasks")
        if coverage["requirement_group_count"] >= substantive_tasks and (
                coverage["greedy_cover_count"] is None or coverage["greedy_cover_count"] < minimum_greedy_cover):
            reasons.append("greedy_cover_below_requested_construction_floor")
        coverage["violations"] = reasons
        topologies[topology] = coverage
        violations.extend({"topology": topology, "reason": reason} for reason in reasons)
    scope = ("Actual selected targets rescoped over the frozen prepared construction pool plus all supplied complete trajectory diagnostics. "
             "Trajectory masks use either-path potential coverage: a saved Python or native success is sufficient to count possible overlap, "
             "so these added masks do not assert confirmed feasibility. Witness counts retain original zero masks and merge only exact "
             "serialized-vector aliases reported under simulator-equivalent numeric hashes; arbitrary nearby vectors are never merged. "
             "Other later measurements and unobserved solutions are outside this evidence. "
             if pool.trajectory_evidence else
             "Actual selected targets rescoped over every design represented in the frozen prepared construction pool. "
             "Later-completed discovery measurements are not included; auditing all currently available measurements remains separate. ")
    return {"passed": not violations, "topologies": topologies, "violations": violations,
            "maximum_single_design_fraction": maximum_coverage_fraction,
            "minimum_greedy_cover": minimum_greedy_cover, "substantive_requirement_groups": substantive_tasks,
            "witness_scope": pool.evidence,
            "trajectory_evidence": deepcopy(pool.trajectory_evidence),
            "scope": scope +
                     "The pool is nonuniform and may contain measurements before reference rounding; fresh admission and release verification remain required. "
                     "Greedy cover is an upper bound on the unknown optimal cover, so greedy >=4 does not prove that at least four solutions are necessary."}


def _cost(audit):
    cap, floor, substantive = (audit["maximum_single_design_fraction"], audit["minimum_greedy_cover"],
                               audit["substantive_requirement_groups"])
    excess = sum(max(0, row["max_single_design_fraction"] - cap) + row["uncovered_task_count"] +
                 (max(0, floor - (row["greedy_cover_count"] or 0)) / floor
                  if row["requirement_group_count"] >= substantive else 0)
                 for row in audit["topologies"].values())
    return len(audit["violations"]), excess


def choose(observations, pool, counts, *, per_topology_limit=35, minimum_distance=0.025,
           maximum_coverage_fraction=0.60, minimum_greedy_cover=4, substantive_tasks=10,
           maximum_repair_steps=20, repair_neighbors=256):
    """Greedy confirmed quotas followed by bounded, same-label swap repair.

    Each observation must carry its admission record. Inputs and baseline
    observations are never mutated or pooled. A partial result is not a proof
    that no feasible selection exists; this search is deliberately bounded.
    """
    if set(counts) != set(LEVELS) or any(type(value) is not int or value < 0 for value in counts.values()) or not sum(counts.values()):
        raise ValueError("Provide nonnegative easy/medium/hard quotas with a positive total.")
    for name, value in (("per_topology_limit", per_topology_limit), ("minimum_greedy_cover", minimum_greedy_cover),
                        ("substantive_tasks", substantive_tasks), ("repair_neighbors", repair_neighbors)):
        if type(value) is not int or value < 1:
            raise ValueError(name + " must be a positive integer.")
    if type(maximum_repair_steps) is not int or maximum_repair_steps < 0:
        raise ValueError("maximum_repair_steps must be a nonnegative integer.")
    if (isinstance(minimum_distance, bool) or not math.isfinite(minimum_distance) or minimum_distance < 0
            or isinstance(maximum_coverage_fraction, bool) or not math.isfinite(maximum_coverage_fraction)
            or not 0 < maximum_coverage_fraction <= 1):
        raise ValueError("Invalid distance or coverage limit.")
    eligible, rejected, seen_ids = [], Counter(), set()
    for observation in observations:
        candidate = observation["candidate"]
        if candidate["id"] in seen_ids:
            raise ValueError("Observations must resolve to one calibration directory per task ID.")
        seen_ids.add(candidate["id"])
        level = observed_level(observation["observation"]["methods"], minimum_trials=10, hard_trials=20)
        if level not in LEVELS:
            rejected["unconfirmed_difficulty_" + level] += 1
            continue
        pool.target_for(candidate)
        failures, reason = admission_failures(candidate, observation.get("admission", {}))
        if reason:
            rejected[reason] += 1
            continue
        curation = start_curation(candidate, level)
        if not curation["start_change_curation_passed"]:
            rejected[curation["rejection_reason"]] += 1
            continue
        eligible.append({**observation, "level": level, "selected_level": level,
                         "initial_clear_failure_metrics": failures, "start_change_curation": curation})
    selected, selected_ids, selected_levels = [], set(), Counter()
    witness_counts, topology_counts = defaultdict(Counter), Counter()
    for level in ("hard", "medium", "easy"):
        while selected_levels[level] < counts[level]:
            options = []
            for row in eligible:
                candidate = row["candidate"]
                if row["level"] != level or candidate["id"] in selected_ids:
                    continue
                if _compatible(candidate, selected, per_topology_limit, minimum_distance):
                    continue
                topology, key = pool.target_for(candidate)
                support, current = pool.coverage_support[(topology, key)], witness_counts[topology]
                after = max(max(current.values(), default=0), max((current[name] + 1 for name in support), default=0))
                concentration = sum(2 * current[name] + 1 for name in support)
                separation = min((distance(candidate["task"], candidate["reference"], other["candidate"]["reference"])
                                  for other in selected if other["candidate"]["topology"] == topology), default=1)
                priority = (topology_counts[topology], after, concentration, -separation, candidate["id"])
                options.append((priority, row))
            if not options:
                break
            _, row = min(options, key=lambda item: item[0])
            selected.append(row)
            selected_ids.add(row["candidate"]["id"])
            selected_levels[level] += 1
            topology, key = pool.target_for(row["candidate"])
            topology_counts[topology] += 1
            witness_counts[topology].update(pool.coverage_support[(topology, key)])
    policy = {"maximum_coverage_fraction": maximum_coverage_fraction, "minimum_greedy_cover": minimum_greedy_cover,
              "substantive_tasks": substantive_tasks}
    audit, repairs = audit_selection(selected, pool, **policy), []
    for _ in range(maximum_repair_steps):
        if audit["passed"]:
            break
        violating = {row["topology"] for row in audit["violations"]}
        neighbors = []
        for index, removed in enumerate(selected):
            if removed["candidate"]["topology"] not in violating:
                continue
            remaining = selected[:index] + selected[index + 1:]
            counts_by_topology = Counter(row["candidate"]["topology"] for row in remaining)
            for replacement in eligible:
                candidate = replacement["candidate"]
                if replacement["level"] != removed["level"] or candidate["id"] in selected_ids:
                    continue
                if _compatible(candidate, remaining, per_topology_limit, minimum_distance):
                    continue
                topology, key = pool.target_for(candidate)
                # Cheap deterministic pre-order bounds the cost of exact audits.
                support = pool.coverage_support[(topology, key)]
                concentration = sum(len(support & pool.coverage_support[pool.target_for(row["candidate"])])
                                    for row in remaining if row["candidate"]["topology"] == topology)
                neighbors.append(((counts_by_topology[topology], concentration, candidate["id"], removed["candidate"]["id"]),
                                  index, replacement))
        best = None
        for _, index, replacement in sorted(neighbors, key=lambda row: row[0])[:repair_neighbors]:
            trial = selected[:index] + [replacement] + selected[index + 1:]
            trial_audit = audit_selection(trial, pool, **policy)
            if _cost(trial_audit) < _cost(audit) and (best is None or _cost(trial_audit) < _cost(best[2])):
                best = index, replacement, trial_audit
        if best is None:
            break
        index, replacement, audit = best
        removed_id = selected[index]["candidate"]["id"]
        selected[index] = replacement
        selected_ids.remove(removed_id)
        selected_ids.add(replacement["candidate"]["id"])
        repairs.append({"removed": removed_id, "added": replacement["candidate"]["id"], "difficulty": replacement["level"]})
    # Recheck all identity and geometric gates on the actual final subset.
    gate_errors, accepted = [], []
    for row in selected:
        reason = _compatible(row["candidate"], accepted, per_topology_limit, minimum_distance)
        if reason:
            gate_errors.append({"id": row["candidate"]["id"], "reason": reason})
        accepted.append(row)
    actual_levels = Counter(row["level"] for row in selected)
    shortfalls = {level: counts[level] - actual_levels[level] for level in LEVELS}
    selection_rejections = Counter(_compatible(row["candidate"], selected, per_topology_limit, minimum_distance)
                                   or "quota_filled_or_bounded_search" for row in eligible if row["candidate"]["id"] not in selected_ids)
    minimum_observed = {}
    for topology in sorted({row["candidate"]["topology"] for row in selected}):
        same = [row["candidate"] for row in selected if row["candidate"]["topology"] == topology]
        minimum_observed[topology] = min((distance(first["task"], first["reference"], second["reference"])
                                         for i, first in enumerate(same) for second in same[i + 1:]), default=None)
    report = {"complete": not any(shortfalls.values()) and audit["passed"] and not gate_errors,
              "requested": dict(counts), "selected_levels": {level: actual_levels[level] for level in LEVELS},
              "shortfalls": shortfalls, "eligible_levels": dict(Counter(row["level"] for row in eligible)),
              "rejected_admissions": dict(rejected), "selected_topologies": dict(Counter(row["candidate"]["topology"] for row in selected)),
              "rejected_by_selection": dict(selection_rejections),
              "unique_reference_count": len({(row["candidate"]["topology"], canonical_hash(row["candidate"]["reference"])) for row in selected}),
              "unique_requirement_group_count": len({row["candidate"]["requirement_group"] for row in selected}),
              "minimum_observed_reference_distance_by_topology": minimum_observed,
              "identity_and_distance_gate_errors": gate_errors, "minimum_normalized_reference_distance": minimum_distance,
              "per_topology_limit": per_topology_limit, "coverage": audit, "repairs": repairs,
              "search": {"method": "deterministic quota greedy then bounded same-difficulty swaps",
                         "maximum_repair_steps": maximum_repair_steps, "repair_neighbors": repair_neighbors,
                         "partial_is_not_infeasibility_proof": True},
              "difficulty_policy": "Unchanged observed_level: minimum 10 seeds/method; hard requires 20 seeds/method. "
                                   "No coverage statistic or construction hint assigns difficulty.",
              "declared_baselines": list(selection_policy.METHODS),
              "start_change_curation_policy": {
                  "scope": "Additional exclusion filters after measured classification; failed filters never relabel tasks or prove absolute hardness.",
                  "continuous_change_threshold": SUBSTANTIVE_CONTINUOUS_CHANGE,
                  "continuous_change_coordinates": "normalized log for positive bounds, normalized linear otherwise",
                  "integer_change_threshold": 1, "medium_minimum_substantive_controls": 2,
                  "hard_minimum_substantive_controls": 3, "hard_required_start_variant": "distant",
                  "easy": "Any admitted, fully calibrated start; no minimum control-count filter."},
              "fresh_release_audit": "pending", "independent_review": "pending", "training_approved": False}
    return selected, report


def _diagnostic_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key in trajectory coverage evidence: " + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_pairs)


def _trajectory_sources(paths, candidates):
    """Capture reports before reading their complete, immutable input manifests."""
    paths = [Path(path).resolve() for path in paths]
    if len(set(paths)) != len(paths):
        raise ValueError("Trajectory report paths must be distinct.")
    report_guard = InputGuard(paths)
    reports, declared = [], {}
    required = {(candidates / name).resolve() for name in ("index.json", "targets.json")}
    for path in paths:
        report = _diagnostic_json(path)
        inputs = report.get("inputs_sha256")
        if not isinstance(inputs, dict) or not inputs:
            raise ValueError("Trajectory diagnostics require a complete input hash manifest.")
        normalized = {}
        for filename, expected in inputs.items():
            if not isinstance(filename, str) or not Path(filename).is_absolute() or not _sha256(expected):
                raise ValueError("Trajectory input identities must be absolute paths and SHA-256 hashes.")
            resolved = Path(filename).resolve()
            if resolved in normalized:
                raise ValueError("Duplicate normalized trajectory input path.")
            normalized[resolved] = expected
            if resolved in declared and declared[resolved] != expected:
                raise ValueError("Trajectory diagnostics bind conflicting input versions.")
            declared[resolved] = expected
        if required - set(normalized):
            raise ValueError("Trajectory diagnostic does not bind the current candidate index and frozen targets.")
        for field, expected_path in (("candidate_index", candidates / "index.json"), ("targets_file", candidates / "targets.json")):
            if field in report and (not isinstance(report[field], str) or
                                    Path(report[field]).resolve() != expected_path.resolve()):
                raise ValueError("Trajectory diagnostic declares a different " + field + ".")
        if "curation_report" in report and (not isinstance(report["curation_report"], str) or
                                           Path(report["curation_report"]).resolve() not in normalized):
            raise ValueError("Trajectory diagnostic must hash its declared curation report.")
        reports.append(report)
    return paths, reports, declared, report_guard


def _bind_trajectory_targets(pool, reports, frozen_targets, candidates, used_topologies):
    indexed = {}
    for target in frozen_targets:
        identity = (target["topology"], target["key"])
        if identity in indexed:
            raise ValueError("Duplicate frozen candidate target definition.")
        indexed[identity] = target
    required = defaultdict(set)
    for candidate in candidates:
        if candidate["topology"] in used_topologies:
            topology, key = pool.target_for(candidate)
            required[topology].add(key)
    for report in reports:
        for topology, row in report.get("topologies", {}).items():
            for key in row.get("target_keys_in_bit_order", []):
                identity = (topology, key)
                target, prepared = indexed.get(identity), pool.targets.get(identity)
                if target is None or prepared is None or any(target.get(field) != prepared.get(field) for field in (
                        "key", "parameters", "constraints", "requirement_group", "empirical_solution_signature")):
                    raise ValueError("Trajectory target definition differs from the frozen prepared target.")
    return required


def select_files(candidates, calibration_directories, targets_path, output, *, curation_report=None,
                 trajectory_coverage=None, counts=None, **options):
    """Keep all calibration evidence on disk; select one directory per shared API."""
    candidates, targets_path, output = map(Path, (candidates, targets_path, output))
    if output.exists():
        raise FileExistsError("Selection output must be new.")
    calibration_directories = list(map(Path, calibration_directories))
    index_guard = InputGuard([candidates / "index.json"])
    candidate_index = json.loads((candidates / "index.json").read_text())
    paths = [candidates / "index.json", targets_path, Path(__file__), Path(__file__).with_name("select.py"),
             Path(__file__).with_name("start_designs.py"), Path(__file__).with_name("candidates.py"),
             *[directory / "summary.json" for directory in calibration_directories]]
    if curation_report is not None:
        paths.append(Path(curation_report))
    trajectory_paths, trajectory_reports, trajectory_inputs, trajectory_guard = _trajectory_sources(
        trajectory_coverage or [], candidates)
    paths.extend([*trajectory_paths, *trajectory_inputs])
    # Capture directory artifacts before the shared loader reads them.
    for entry in candidate_index["tasks"]:
        candidate_path = (candidates / entry["path"]).resolve()
        if not candidate_path.is_relative_to(candidates.resolve()):
            raise ValueError("Candidate artifact escapes its indexed directory.")
        paths.append(candidate_path)
    paths.extend(path for directory in calibration_directories for path in sorted(directory.glob("*/admission.json")))
    guard = InputGuard(paths)
    index_guard.assert_unchanged()
    trajectory_guard.assert_unchanged()
    for path, expected in trajectory_inputs.items():
        if guard.hashes[str(path)] != expected:
            raise ValueError("Stale trajectory diagnostic input hash: " + str(path))
    data = json.loads(targets_path.read_text())
    pool = PreparedPool(data["targets"] if isinstance(data, dict) else data,
                        json.loads(Path(curation_report).read_text()) if curation_report is not None else None)
    observations = load_observations(candidates, calibration_directories, minimum_trials=10, hard_trials=20)
    if trajectory_reports:
        if curation_report is None:
            raise ValueError("Trajectory coverage requires --curation-report to retain all original witness identities.")
        candidate_rows = []
        for entry in candidate_index["tasks"]:
            row = json.loads((candidates / entry["path"]).read_text())
            if row["id"] != entry["id"] or row["task"]["id"] != entry["id"]:
                raise ValueError("Candidate identity differs from the frozen index.")
            candidate_rows.append(row)
        frozen = _diagnostic_json(candidates / "targets.json")
        required = _bind_trajectory_targets(pool, trajectory_reports, frozen["targets"], candidate_rows,
            {row["candidate"]["topology"] for row in observations})
        pool.add_trajectory_coverage(trajectory_reports, required)
    for row in observations:
        path = Path(row["calibration_directory"]) / row["candidate"]["id"] / "admission.json"
        raw = json.loads(path.read_text())
        # The existing checkpoint loader verifies both identity and payload hashes.
        row["admission"] = load_checkpoint(path, raw.get("_checkpoint", {}).get("identity")) or {}
    selected, report = choose(observations, pool, counts or {"easy": 100, "medium": 125, "hard": 25}, **options)
    history = defaultdict(list)
    for directory in calibration_directories:
        for row in json.loads((directory / "summary.json").read_text())["tasks"]:
            history[row["id"]].append({"calibration_directory": str(directory), "admitted": row["admitted"],
                                       "observed_level": observed_level(row["methods"], minimum_trials=10, hard_trials=20),
                                       "methods": deepcopy(row["methods"])})
    guard.assert_unchanged()
    result = {"status": "selection_complete" if report["complete"] else "selection_partial", "report": report,
              "ids": [row["candidate"]["id"] for row in selected],
              "tasks": [{"id": row["candidate"]["id"], "difficulty": row["level"], "observed_level": row["level"],
                         "topology": row["candidate"]["topology"], "calibration_directory": row["calibration_directory"],
                         "initial_clear_failure_metrics": row["initial_clear_failure_metrics"],
                         **row["start_change_curation"]} for row in selected],
              "inputs_sha256": guard.hashes, "calibration_observations": dict(history),
              "trajectory_coverage_sources": [{"path": str(path), "sha256": guard.hashes[str(path)],
                  "inputs_sha256": deepcopy(diagnostic["inputs_sha256"]),
                  "coverage_semantics": "either-path potential coverage; not a confirmed feasibility claim"}
                  for path, diagnostic in zip(trajectory_paths, trajectory_reports)],
              "calibration_policy": "Preserve every supplied observation; do not pool seeds across stages. "
                                    "The shared load_observations API uses the last qualifying directory per ID. "
                                    "Supply final independent confirmation directories last. This is not a seed-disjointness audit.",
              "training_approved": False}
    save_json(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, action="append", required=True)
    parser.add_argument("--targets", type=Path, required=True, help="Complete prepared targets.json, not the truncated starts list.")
    parser.add_argument("--curation-report", type=Path)
    parser.add_argument("--trajectory-coverage", type=Path, action="append",
                        help="Complete immutable trajectory diagnostic; repeat for additional evidence. Requires --curation-report.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--counts", nargs=3, type=int, default=[100, 125, 25], metavar=("EASY", "MEDIUM", "HARD"))
    parser.add_argument("--per-topology-limit", type=int, default=35)
    parser.add_argument("--minimum-distance", type=float, default=0.025)
    parser.add_argument("--maximum-coverage-fraction", type=float, default=0.60)
    parser.add_argument("--minimum-greedy-cover", type=int, default=4)
    parser.add_argument("--substantive-tasks", type=int, default=10)
    parser.add_argument("--maximum-repair-steps", type=int, default=20)
    args = parser.parse_args()
    result = select_files(args.candidates, args.calibration, args.targets, args.output,
                          curation_report=args.curation_report, trajectory_coverage=args.trajectory_coverage,
                          counts=dict(zip(LEVELS, args.counts)),
                          per_topology_limit=args.per_topology_limit, minimum_distance=args.minimum_distance,
                          maximum_coverage_fraction=args.maximum_coverage_fraction, minimum_greedy_cover=args.minimum_greedy_cover,
                          substantive_tasks=args.substantive_tasks, maximum_repair_steps=args.maximum_repair_steps)
    print(json.dumps({"status": result["status"], "selected": result["report"]["selected_levels"],
                      "shortfalls": result["report"]["shortfalls"], "coverage_passed": result["report"]["coverage"]["passed"]}, indent=2))


if __name__ == "__main__":
    main()
