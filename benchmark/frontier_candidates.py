"""Construct joint sizing targets with measured margins and diverse feasible sets.

These are project-authored nominal requirements, not recovered paper numbers.
Construction labels are hints only; fresh admission and baseline calibration are
required. No reference or discovery-pool coverage statistic proves difficulty.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path

from analog_design.metrics import score
from analog_design.simulator import ROOT, digest
from benchmark.calibrate import CODE_FILES, InputGuard
from benchmark.candidates import (METRICS, distance, load_designs,
                                  reference_margin, requirement_identity)
from benchmark.explore import rounded_parameters
from benchmark.research.probe_amplifiers import parse_parameters
from benchmark.simulation import canonical_hash, save_json


PREFERRED = (1, 1.2, 1.5, 1.8, 2, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)
GRID = sorted({factor * 10.0 ** exponent for exponent in range(-12, 10)
               for factor in PREFERRED})
MINIMUM_METRICS = {"gain_db", "unity_gain_hz", "phase_margin_deg"}
ABSOLUTE_MARGIN = {"gain_db": 0.2, "unity_gain_hz": 10, "phase_margin_deg": 0.3,
                   "power_w": 1e-8, "dc_error_v": 4e-6,
                   "max_tracking_error_v": 4e-6,
                   "settling_rise_s": 12e-9, "settling_fall_s": 12e-9}
MINIMUM_REFERENCE_DISTANCE = 0.025
START_VARIANTS = ("nearest", "intermediate", "distant")


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _domain(task):
    if not task.get("parameters"):
        raise ValueError("A target domain needs editable parameters.")
    for name, rule in task["parameters"].items():
        if (not _number(rule["min"]) or not _number(rule["max"])
                or not 0 < rule["min"] < rule["max"]):
            raise ValueError("Positive nondegenerate bounds are required: " + name)
    if set(task["constraints"]) != set(METRICS):
        raise ValueError("The target domain must define all eight metrics.")
    for name, limits in task["constraints"].items():
        direction = "min" if name in MINIMUM_METRICS else "max"
        if (set(limits) != {direction} or not _number(limits[direction])
                or limits[direction] <= 0):
            raise ValueError("Invalid domain requirement: " + name)


def _valid_row(task, row):
    if row.get("status") != "ok":
        return False
    values, measured = row.get("parameters", {}), row.get("metrics", {})
    if set(values) != set(task["parameters"]):
        return False
    for name, rule in task["parameters"].items():
        value = values[name]
        if (not _number(value) or not rule["min"] <= value <= rule["max"]
                or rule.get("integer") and value != int(value)):
            return False
    return all(name in measured and _number(measured[name]) for name in METRICS)


def _pool(task, designs):
    """Prefer an exactly rounded measurement when several rows round alike."""
    by_key = {}
    for row in designs:
        if not _valid_row(task, row):
            continue
        values = rounded_parameters(task, row["parameters"])
        key = canonical_hash(values)
        priority = (row["parameters"] != values, canonical_hash(row["parameters"]),
                    row.get("measurement_directory", ""))
        if key not in by_key or priority < by_key[key][0]:
            by_key[key] = (priority, {**row, "rounded_parameters": values, "design_key": key})
    return [by_key[key][1] for key in sorted(by_key)]


def tight_constraints(task, metrics):
    """Round outward from target-relative 2% and absolute numerical margins."""
    if not all(name in metrics and _number(metrics[name]) for name in METRICS):
        return None
    constraints = {}
    for name in METRICS:
        minimum = name in MINIMUM_METRICS
        direction = "min" if minimum else "max"
        absolute = ABSOLUTE_MARGIN[name]
        if name in {"gain_db", "phase_margin_deg"}:
            target = math.floor(metrics[name] - absolute)
        elif minimum:
            wanted = min(metrics[name] / 1.02, metrics[name] - absolute)
            target = max((value for value in GRID if value <= wanted), default=None)
        else:
            wanted = max(metrics[name] / 0.98, metrics[name] + absolute)
            target = min((value for value in GRID if value >= wanted), default=None)
        if target is None:
            return None
        floor = task["constraints"][name][direction]
        constraints[name] = {direction: max(target, floor) if minimum else min(target, floor)}
    return constraints if reference_margin(metrics, constraints) else None


def _dominates(first, second):
    oriented = [(first[name], second[name]) if name in MINIMUM_METRICS
                else (-first[name], -second[name]) for name in METRICS]
    return all(a >= b for a, b in oriented) and any(a > b for a, b in oriented)


def coverage_report(targets, pool):
    """Measure coverage using every valid characterized design, not just refs."""
    masks = []
    for row in pool:
        mask = sum(1 << index for index, target in enumerate(targets)
                   if score(row["metrics"], target["constraints"])["success"])
        masks.append((row["design_key"], mask))
    remaining = (1 << len(targets)) - 1
    greedy = []
    while remaining:
        key, mask = max(masks, key=lambda item: ((item[1] & remaining).bit_count(), item[0]))
        additional = (mask & remaining).bit_count()
        if not additional:
            break
        greedy.append({"design_key": key, "newly_covered": additional})
        remaining &= ~mask
    worst_key, worst_mask = max(masks, key=lambda item: (item[1].bit_count(), item[0]),
                               default=(None, 0))
    maximum = worst_mask.bit_count()
    return {"task_count": len(targets), "characterized_design_count": len(pool),
            "max_single_design_coverage": maximum,
            "max_single_design_fraction": maximum / len(targets) if targets else None,
            "worst_covering_design_key": worst_key,
            "greedy_reference_cover": len(greedy) if not remaining else None,
            "uncovered_task_count": remaining.bit_count(), "greedy_steps": greedy,
            "coverage_bitmasks_hex": {key: hex(mask) for key, mask in masks},
            "target_keys_in_bit_order": [target["key"] for target in targets],
            "scope": "Coverage of this nonuniform measured pool; not a solve probability or minimum set-cover proof."}


def choose_targets(entry, designs, count=50):
    """Return (targets, report), retaining honest shortfalls instead of padding.

    Callers must use load_designs for archived source/measurement validation.
    This pure constructor also checks finite values, bounds, public defaults,
    robust references, distinct feasible sets and normalized ref separation.
    """
    if type(count) is not int or count < 1:
        raise ValueError("Requested target count must be a positive integer.")
    task = entry["task"]
    _domain(task)
    public = entry.get("public_default", {})
    if set(public) != set(task["parameters"]):
        raise ValueError("The public default must contain every editable parameter.")
    for name, rule in task["parameters"].items():
        value = public[name]
        if (not _number(value) or not rule["min"] <= value <= rule["max"]
                or rule.get("integer") and value != int(value)):
            raise ValueError("Public default is outside the declared domain: " + name)
    pool = _pool(task, designs)
    defaults = rounded_parameters(task, public)
    default_rows = [row for row in pool if row["rounded_parameters"] == defaults]
    if not default_rows:
        raise ValueError("Missing measured public default for " + entry["name"])
    rejected = Counter()
    eligible = [row for row in pool if reference_margin(row["metrics"], task["constraints"])]
    pareto = {row["design_key"] for row in eligible if not any(
        _dominates(other["metrics"], row["metrics"]) for other in eligible)}
    proposed = []
    for row in eligible:
        constraints = tight_constraints(task, row["metrics"])
        if constraints is None:
            rejected["insufficient_grid_margin"] += 1
            continue
        if any(score(default["metrics"], constraints)["success"] for default in default_rows):
            rejected["public_default_passes"] += 1
            continue
        support = [item["design_key"] for item in pool
                   if score(item["metrics"], constraints)["success"]]
        definition = {**task, "constraints": constraints}
        proposed.append({
            "topology": entry["name"], "key": row["design_key"],
            "characterization_key": canonical_hash(row["parameters"]),
            "parameters": row["rounded_parameters"], "constraints": constraints,
            "metrics": deepcopy(row["metrics"]),
            "measurement_directory": row["measurement_directory"],
            "measured_parameters": deepcopy(row["parameters"]),
            "reference_measured_after_rounding": row["parameters"] == row["rounded_parameters"],
            "measured_pareto": row["design_key"] in pareto,
            "requirement_group": canonical_hash(requirement_identity(definition)),
            "empirical_solution_signature": canonical_hash(support),
            "empirical_feasible_design_keys": support,
            "empirical_pool_feasible_count": len(support),
            "empirical_pool_valid_count": len(pool),
        })
    selected, seen_groups, seen_signatures = [], set(), set()
    coverage = Counter()
    remaining = list(proposed)
    while len(selected) < count and remaining:
        options = []
        for target in remaining:
            if target["requirement_group"] in seen_groups or target["empirical_solution_signature"] in seen_signatures:
                continue
            separation = min((distance(task, target["parameters"], other["parameters"])
                              for other in selected), default=1.0)
            if separation < MINIMUM_REFERENCE_DISTANCE:
                continue
            support = target["empirical_feasible_design_keys"]
            maximum = max(max(coverage.values(), default=0),
                          max((coverage[key] + 1 for key in support), default=0))
            added_square = sum(2 * coverage[key] + 1 for key in support)
            options.append(((maximum, added_square, -separation, target["key"]), target))
        if not options:
            break
        _, target = min(options, key=lambda item: item[0])
        remaining.remove(target)
        selected.append(target)
        seen_groups.add(target["requirement_group"])
        seen_signatures.add(target["empirical_solution_signature"])
        coverage.update(target["empirical_feasible_design_keys"])
    report = {
        "topology": entry["name"], "requested_count": count,
        "selected_count": len(selected), "complete": len(selected) == count,
        "input_rows": len(designs), "valid_distinct_rounded_designs": len(pool),
        "robust_base_designs": len(eligible), "measured_pareto_designs": len(pareto),
        "tight_target_count": len(proposed),
        "distinct_feasible_sets": len({item["empirical_solution_signature"] for item in proposed}),
        "rejections": dict(rejected),
        "minimum_normalized_reference_distance": MINIMUM_REFERENCE_DISTANCE,
        "actual_minimum_reference_distance": min((distance(task, a["parameters"], b["parameters"])
            for i, a in enumerate(selected) for b in selected[i + 1:]), default=None),
        "coverage": coverage_report(selected, pool),
        "calibration_status": "not_calibrated", "training_approved": False,
    }
    return selected, report


def clear_failures(metrics, constraints):
    """Exclude starts whose only failure is within a numerical margin."""
    failures = []
    for name, limits in constraints.items():
        value = metrics.get(name)
        if not _number(value):
            return []
        direction, target = next(iter(limits.items()))
        violation = target - value if direction == "min" else value - target
        required = max(ABSOLUTE_MARGIN[name], 0 if name in {"gain_db", "phase_margin_deg"} else 0.02 * target)
        if violation >= required:
            failures.append(name)
    return failures


def substantive_changes(task, first, second):
    """Identify actual changed controls using the declared domain coordinates.

    Integer controls need a full integer step; continuous controls need 0.025
    of their normalized log range (linear for nonpositive domains). Construction
    tags and requested perturbation factors are deliberately not consulted.
    """
    changed = []
    for name, rule in task["parameters"].items():
        low, high = rule["min"], rule["max"]
        if low == high:
            continue
        if rule.get("integer"):
            substantial = abs(first[name] - second[name]) >= 1
        else:
            delta = (abs(math.log(first[name] / second[name])) / math.log(high / low)
                     if low > 0 else abs(first[name] - second[name]) / (high - low))
            substantial = delta >= 0.025
        if substantial:
            changed.append(name)
    return sorted(changed)


def build_candidates(entry, designs, targets, starts=None, *, variants=START_VARIANTS):
    """Emit distinct starting designs for each fixed requirement/reference pair.

    Nearest prefers target-tagged controlled starts. Intermediate prefers actual
    two-control controlled starts, then eligible global starts. Distant uses the
    global pool. Construction hints never assign calibrated difficulty. Final
    selection must retain only one candidate per reference/requirement group.
    """
    if not variants or len(set(variants)) != len(variants) or set(variants) - set(START_VARIANTS):
        raise ValueError("Choose distinct nearest/intermediate/distant start variants.")
    template = entry["task"]
    candidates, rejected, covered = [], Counter(), 0
    variant_counts = Counter()
    for target in targets:
        options = []
        available = [(row, "global") for row in designs]
        available += [(row, "controlled") for row in starts or []
                      if row.get("target_key") in {target["key"], target["characterization_key"]}]
        for row, origin in available:
            if not _valid_row(template, row):
                continue
            failures = clear_failures(row["metrics"], target["constraints"])
            if not failures:
                continue
            values = rounded_parameters(template, row["parameters"])
            if values == target["parameters"]:
                continue
            result = score(row["metrics"], target["constraints"])
            failed = sum(not passed for passed in result["checks"].values())
            separation = distance(template, values, target["parameters"])
            options.append({"row": row, "origin": origin, "score": result,
                            "failures": failures, "failed": failed, "distance": separation,
                            "substantive_changes": substantive_changes(template, values, target["parameters"]),
                            "key": canonical_hash(values)})
        if not options:
            rejected["no_clear_valid_failing_start"] += 1
            continue
        used = set()
        for variant in variants:
            eligible = [item for item in options if item["key"] not in used
                        and (variant != "distant" or item["origin"] == "global")
                        and (variant != "intermediate" or len(item["substantive_changes"]) >= 2
                             and (item["origin"] == "controlled" or len(item["failures"]) >= 2))]
            if not eligible:
                rejected["missing_distinct_" + variant + "_start"] += 1
                continue

            def priority(item):
                if variant == "nearest":
                    return (item["origin"] != "controlled", item["failed"] != 1,
                            item["distance"], -item["score"]["reward"], item["key"])
                if variant == "intermediate":
                    return (item["origin"] != "controlled", abs(item["distance"] - 0.2), abs(item["failed"] - 2),
                            -item["score"]["reward"], item["key"])
                return (-item["distance"], item["failed"] < 2, item["score"]["reward"], item["key"])

            selected = min(eligible, key=priority)
            used.add(selected["key"])
            variant_counts[variant] += 1
            start, initial_score, failures = selected["row"], selected["score"], selected["failures"]
            hint = {"nearest": "easy", "intermediate": "medium", "distant": "hard"}[variant]
            task = deepcopy(template)
            task["constraints"] = deepcopy(target["constraints"])
            task["initial_parameters"] = rounded_parameters(template, start["parameters"])
            task["purpose"] = (f"Size the {entry['name']} amplifier using the permitted parameters. "
                               "Meet all eight fixed nominal DC, AC, and transient requirements within 30 evaluations. "
                               "The joint requirements are project-defined for this released circuit adaptation.")
            fingerprint = canonical_hash({**requirement_identity(task), "initial_parameters": task["initial_parameters"]})
            task["id"] = entry["name"].lower().replace("_pin_3", "") + "_frontier_" + fingerprint[:16]
            perturbation = {key: deepcopy(start[key]) for key in
                            ("perturbation_kind", "requested_factors", "actual_factors", "changed_controls",
                             "normalized_reference_distance") if key in start}
            candidates.append({
                "id": task["id"], "topology": entry["name"], "topology_family": entry["family"],
                "proposed_difficulty": hint, "profile": "balanced", "task": task,
                "reference": deepcopy(target["parameters"]), "target_key": target["key"],
                "requirement_group": target["requirement_group"], "fingerprint": fingerprint,
                "characterization_reference": target["measurement_directory"],
                "characterization_start": start["measurement_directory"],
                "characterization_initial_failures": [name for name, passed in initial_score["checks"].items() if not passed],
                "characterization_initial_clear_failures": failures,
                "empirical_pool_feasible_count": target["empirical_pool_feasible_count"],
                "empirical_pool_valid_count": target["empirical_pool_valid_count"],
                "empirical_solution_signature": target["empirical_solution_signature"],
                "generation_mode": "source_grounded_measured_joint_target_curation",
                "start_variant": variant, "start_pool": selected["origin"],
                "start_normalized_reference_distance": selected["distance"],
                "substantive_changed_controls": selected["substantive_changes"],
                "start_perturbation": perturbation or deepcopy(start.get("perturbation")),
                "initial_measured_after_rounding": start["parameters"] == task["initial_parameters"],
                "reference_measured_after_rounding": target["reference_measured_after_rounding"],
                "calibration_status": "pending", "training_approved": False,
            })
        covered += bool(used)
    return candidates, {"targets": len(targets), "candidates": len(candidates),
                        "targets_with_candidates": covered, "variant_counts": dict(variant_counts),
                        "requested_variants": list(variants), "rejections": dict(rejected),
                        "complete": covered == len(targets),
                        "all_variants_available": len(candidates) == len(targets) * len(variants),
                        "final_selection_rule": "One candidate per reference vector and requirement group."}


def load_starts(directories, entry):
    # load_designs verifies every row's archived source, settings and metrics.
    # Re-read tagged rows after validation: its parameter deduplication would
    # otherwise discard a shared start's association with another target.
    paths = [Path(directory) / (entry["name"] + ".json") for directory in directories]
    guard = InputGuard([path for path in paths if path.is_file()])
    valid = load_designs(directories, entry)
    keys = {canonical_hash(row["parameters"]) for row in valid}
    rows = []
    for directory in directories:
        path = Path(directory) / (entry["name"] + ".json")
        if not path.is_file():
            continue
        for row in json.loads(path.read_text()):
            parameters = {**entry["public_default"], **row.get("parameters", {})}
            if row.get("status") == "ok" and canonical_hash(parameters) in keys:
                rows.append({**row, "parameters": parameters})
    guard.assert_unchanged()
    return rows


def _validate_target_archive(entry, target):
    """Bind a saved target to its original source and measurement evidence."""
    task = entry["task"]
    directory = (ROOT / target["measurement_directory"]).resolve()
    if not directory.is_relative_to((ROOT / "runs").resolve()):
        raise ValueError("Saved target evidence is outside the trusted runs directory.")
    circuit = ROOT / task["circuit_directory"]
    paths = {"netlist_sha256": circuit / "netlist.spice",
             "reference_parameters_sha256": circuit / "reference.params",
             "parameters_sha256": directory / "parameters.spice",
             "evaluator_sha256": ROOT / "analog_design/simulator.py",
             "metrics_code_sha256": ROOT / "analog_design/metrics.py",
             "crosscheck_code_sha256": ROOT / "analog_design/crosscheck.py"}
    guard = InputGuard([directory / "result.json", directory / "task.json", *paths.values()])
    evidence = json.loads((directory / "result.json").read_text())
    measured_task = json.loads((directory / "task.json").read_text())
    # Earlier probes used copied circuits, narrower bounds and some controls
    # fixed at their public values. Read the archived parameter deck to verify
    # those formerly fixed values explicitly. Current bounds were checked above.
    physical = parse_parameters(directory / "parameters.spice")
    recorded = evidence.get("parameters", {})
    effective = {name: recorded.get(name, physical.get(name)) for name in task["parameters"]}
    declarations_agree = all(name in physical and math.isclose(physical[name], value, rel_tol=1e-14, abs_tol=0)
                             for name, value in effective.items() if _number(value))
    if (evidence.get("status") != "ok" or evidence.get("metrics") != target["metrics"]
            or effective != target["measured_parameters"] or not declarations_agree
            or any(evidence.get("provenance", {}).get(key) != digest(path) for key, path in paths.items())
            or any(measured_task.get(key) != task[key] for key in ("subcircuit", "conditions", "ac", "transient"))):
        raise ValueError("Saved target source, conditions, parameters, or metrics disagree with its archive.")
    guard.assert_unchanged()


def load_fixed_targets(path, entries, *, maximum_per_topology=None):
    """Validate saved target definitions without selecting again from a new pool.

    Discovery support counts remain statistics of the original saved pool.
    Rounded references still require fresh admission and final verification.
    """
    if maximum_per_topology is not None and (type(maximum_per_topology) is not int or maximum_per_topology < 1):
        raise ValueError("maximum_per_topology must be a positive integer.")
    path = Path(path)
    guard = InputGuard([path])
    data = json.loads(path.read_text())
    targets = data.get("targets") if isinstance(data, dict) else data
    if not isinstance(targets, list) or not targets:
        raise ValueError("Saved targets must be a nonempty list.")
    domains = {entry["name"]: entry for entry in entries}
    selected, seen_keys, seen_groups, counts = [], set(), set(), Counter()
    for target in targets:
        name = target.get("topology")
        if name not in domains:
            raise ValueError("Saved target has an unknown topology.")
        entry, task = domains[name], domains[name]["task"]
        _domain(task)
        measured = {"status": "ok", "parameters": target["measured_parameters"], "metrics": target["metrics"]}
        if not _valid_row(task, measured):
            raise ValueError("Saved target has invalid measured parameters or metrics.")
        rounded = rounded_parameters(task, target["measured_parameters"])
        if (target["parameters"] != rounded or target["key"] != canonical_hash(rounded)
                or target["characterization_key"] != canonical_hash(target["measured_parameters"])
                or type(target["reference_measured_after_rounding"]) is not bool
                or target["reference_measured_after_rounding"] != (rounded == target["measured_parameters"])):
            raise ValueError("Saved target reference rounding or characterization identity disagrees.")
        if target["constraints"] != tight_constraints(task, target["metrics"]):
            raise ValueError("Saved target constraints disagree with its measured joint requirements.")
        group = canonical_hash(requirement_identity({**task, "constraints": target["constraints"]}))
        if target["requirement_group"] != group:
            raise ValueError("Saved target requirement identity disagrees with its domain.")
        identity = (name, target["key"])
        if identity in seen_keys or group in seen_groups:
            raise ValueError("Saved targets repeat a reference or requirement group.")
        support = target["empirical_feasible_design_keys"]
        if (not isinstance(support, list) or not support or support != sorted(set(support))
                or target["key"] not in support or canonical_hash(support) != target["empirical_solution_signature"]
                or type(target["empirical_pool_feasible_count"]) is not int
                or target["empirical_pool_feasible_count"] != len(support)
                or type(target["empirical_pool_valid_count"]) is not int
                or target["empirical_pool_valid_count"] < len(support)):
            raise ValueError("Saved target discovery support metadata is inconsistent.")
        _validate_target_archive(entry, target)
        seen_keys.add(identity)
        seen_groups.add(group)
        if maximum_per_topology is None or counts[name] < maximum_per_topology:
            selected.append(deepcopy(target))
            counts[name] += 1
    guard.assert_unchanged()
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "benchmark/domains.json")
    parser.add_argument("--designs", type=Path, action="append", required=True)
    parser.add_argument("--starts", type=Path, action="append")
    parser.add_argument("--targets-file", type=Path,
                        help="Use and validate these exact saved targets; never select replacement targets.")
    parser.add_argument("--maximum-targets-per-topology", type=int,
                        help="Retain the first N saved targets per topology, matching controlled-start generation.")
    parser.add_argument("--variants", nargs="+", choices=START_VARIANTS, default=list(START_VARIANTS),
                        help="Starting-design constructions to emit; these are not calibrated levels.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-topology", type=int, default=80)
    parser.add_argument("--names", nargs="*")
    parser.add_argument("--targets-only", action="store_true")
    args = parser.parse_args()
    if args.per_topology < 1:
        parser.error("--per-topology must be positive.")
    if args.maximum_targets_per_topology is not None and (not args.targets_file or args.maximum_targets_per_topology < 1):
        parser.error("A positive --maximum-targets-per-topology requires --targets-file.")
    if len(set(args.variants)) != len(args.variants):
        parser.error("--variants must be distinct.")
    if args.output.exists():
        raise FileExistsError("Candidate output must be new.")
    entries = json.loads(args.catalog.read_text())["entries"]
    input_paths = [args.catalog, Path(__file__), ROOT / "benchmark/research/probe_amplifiers.py",
                   *[ROOT / path for path in CODE_FILES]]
    if args.targets_file:
        input_paths.append(args.targets_file)
    input_paths += [path for directory in args.designs + (args.starts or [])
                    for entry in entries if (path := directory / (entry["name"] + ".json")).is_file()]
    guard = InputGuard(input_paths)
    fixed_targets = (load_fixed_targets(args.targets_file, entries,
                     maximum_per_topology=args.maximum_targets_per_topology) if args.targets_file else None)
    if args.names:
        if set(args.names) - {entry["name"] for entry in entries}:
            parser.error("--names contains an unknown topology.")
        entries = [entry for entry in entries if entry["name"] in args.names]
    args.output.mkdir(parents=True, exist_ok=False)
    all_targets, reports, index = [], [], []
    for entry in entries:
        designs = load_designs(args.designs, entry)
        if fixed_targets is None:
            targets, report = choose_targets(entry, designs, args.per_topology)
        else:
            targets = [target for target in fixed_targets if target["topology"] == entry["name"]]
            pool = _pool(entry["task"], designs)
            defaults = rounded_parameters(entry["task"], entry["public_default"])
            default_rows = [row for row in pool if row["rounded_parameters"] == defaults]
            if not default_rows:
                raise ValueError("Missing measured public default for " + entry["name"])
            if any(score(row["metrics"], target["constraints"])["success"]
                   for row in default_rows for target in targets):
                raise ValueError("A saved target is solved by the measured public default.")
            report = {"topology": entry["name"], "target_source": str(args.targets_file.resolve()),
                      "target_definitions": "Retained exactly from the validated saved target file.",
                      "selected_count": len(targets), "complete": True,
                      "distinct_feasible_sets": len({target["empirical_solution_signature"] for target in targets}),
                      "coverage": coverage_report(targets, pool),
                      "coverage_scope": "Recomputed on the supplied current pool; saved per-target support metadata describes its original pool.",
                      "calibration_status": "not_calibrated", "training_approved": False}
        all_targets.extend(targets)
        if not args.targets_only:
            starts = load_starts(args.starts, entry) if args.starts else None
            proposed, start_report = build_candidates(entry, designs, targets, starts, variants=args.variants)
            report["starts"] = start_report
            for candidate in proposed:
                relative = "candidates/" + candidate["id"] + ".json"
                save_json(args.output / relative, candidate)
                index.append({key: candidate[key] for key in
                              ("id", "topology", "topology_family", "proposed_difficulty", "profile")})
                index[-1]["path"] = relative
        reports.append(report)
        save_json(args.output / "targets.json", {"schema_version": 1, "status": "targets_not_freshly_verified",
                  "targets": all_targets, "training_approved": False})
        save_json(args.output / "curation_report.json", {"topologies": reports,
                  "status": "targets_only" if args.targets_only else "candidates_not_verified_or_calibrated",
                  "training_approved": False})
        print(json.dumps({"topology": entry["name"], "targets": len(targets),
                          "distinct_feasible_sets": report["distinct_feasible_sets"],
                          "max_single_design_coverage": report["coverage"]["max_single_design_coverage"],
                          "greedy_reference_cover": report["coverage"]["greedy_reference_cover"],
                          "complete": report["complete"]}), flush=True)
    guard.assert_unchanged()
    save_json(args.output / "index.json", {"status": "targets_only" if args.targets_only else "candidates_not_verified_or_calibrated",
              "tasks": index, "training_approved": False})
    save_json(args.output / "generation_manifest.json", {"inputs_sha256": guard.hashes,
              "targets_file": str(args.targets_file.resolve()) if args.targets_file else None,
              "maximum_targets_per_topology": args.maximum_targets_per_topology,
              "variants": args.variants, "candidate_count": len(index),
              "status": "construction_complete_requires_fresh_admission_and_calibration",
              "training_approved": False})


if __name__ == "__main__":
    main()
