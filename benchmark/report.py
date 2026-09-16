"""Generate public benchmark documentation from a bundle and optional audit.

This is a reporter, not a qualification gate or simulator. Private reference
files are hashed for integrity but never parsed; calibration trajectories are
never opened. Source records are projected onto an explicit documentation
allowlist because some contain historical reference parameter vectors.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
from urllib.parse import quote

from benchmark.baselines import METHODS
from benchmark.select import observed_level


ROOT = Path(__file__).resolve().parents[1]
LEVELS = ("easy", "medium", "hard")
SPLITS = ("train", "validation", "test")
BASELINE_DESCRIPTIONS = {
    "uniform_linear": "Independent proposals sampled uniformly in each allowed physical-value range; integer controls remain discrete.",
    "uniform_log": "Independent proposals sampled in declared logarithmic coordinates for positive continuous controls; integer controls remain discrete.",
    "coordinate_search": "Adaptive coordinate search using observed reward, step-size reduction, and bounded restarts.",
    "engineering_sweep": "Seeded control order probes anchored ×0.5 and ×2 changes, combines independently improving moves, then uses incumbent-centered ×1.5 and ×(2/3) coordinate sweeps and bounded exploration as budget permits. It does not read reference solutions or generator metadata.",
}


def _load(path):
    return json.loads(Path(path).read_text())


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _contained(directory, relative):
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("Bundle artifact escapes its directory: " + str(relative))
    return path


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _link(label, path):
    return "[" + _cell(label).replace("[", "\\[").replace("]", "\\]") + "](" + quote(str(path), safe="/:#") + ")"


def _table(headers, rows):
    return "\n".join(["| " + " | ".join(map(_cell, headers)) + " |",
                      "| " + " | ".join("---" for _ in headers) + " |"] +
                     ["| " + " | ".join(map(_cell, row)) + " |" for row in rows])


def _counts(rows, key, choices=None):
    counts = Counter(row[key] for row in rows)
    return {name: counts[name] for name in choices or sorted(counts)}


def _source_summary(topology, path, source):
    release = source.get("release", {})
    paper = release.get("paper", source.get("paper", {}))
    if isinstance(paper, str):
        paper = {"title": paper, "source_url": source.get("source_url")}
    doi = paper.get("doi")
    limitations = list(source.get("limitations", [])) + list(release.get("review_limitations", []))
    if release.get("notes"):
        limitations.append(release["notes"])
    level = paper.get("review_level", source.get("review_level"))
    if not level:
        level = "not_declared_in_bundled_source"
        if source.get("method"):
            limitations.append("Recorded inspection method: " + source["method"])
    return {"topology": topology, "record": path,
            "title": paper.get("title", "Paper title not recorded"),
            "year": paper.get("year"), "doi": doi,
            "url": "https://doi.org/" + doi if doi else paper.get("source_url", source.get("source_url")),
            "review_level": level,
            "review_limitations": limitations,
            "claim_level": release.get("claim_level", "Project adaptation; original performance not reproduced."),
            "repository": source.get("repository"), "commit": source.get("commit"),
            "license": source.get("license", "See repository third-party notices and the bundled source record."),
            "adaptations": list(source.get("adaptations", source.get("engineering_choices", [])))}


def _pilot_exclusion(bundle, entries):
    pilot_path = ROOT / "datasets/pilot_20260909/index.json"
    if not pilot_path.exists():
        return {"status": "pending", "reason": "Historical pilot index is unavailable."}
    pilot = _load(pilot_path)
    old = pilot.get("tasks", [])
    ids = {row["id"] for row in entries}
    hashes = {row["sha256"] for row in entries}
    overlap_ids = sorted(ids & {row["id"] for row in old})
    overlap_hashes = sorted(hashes & {row["sha256"] for row in old})
    return {"status": "excluded" if not overlap_ids and not overlap_hashes else "overlap",
            "pilot_index": os.path.relpath(pilot_path, bundle),
            "pilot_index_sha256": _digest(pilot_path), "pilot_task_count": len(old),
            "task_id_overlap_count": len(overlap_ids), "task_sha256_overlap_count": len(overlap_hashes),
            "scope": "Exact historical task IDs and serialized task hashes only. This does not establish absence of semantic similarity or prior model exposure."}


def _audit_summary(bundle, index, path):
    result = {"status": "pending", "report": None, "artifact_binding": "not_checked",
              "fresh_simulations": False, "verified_task_count": 0,
              "public_default": {"status": "pending"},
              "reference_diversity": {"status": "pending"},
              "reference_solution_coverage": {"status": "pending"}}
    if path is None:
        return result, {}
    path = Path(path).resolve()
    audit = _load(path)
    result["report"] = os.path.relpath(path, bundle)
    result["report_sha256"] = _digest(path)
    result["reported_status"] = audit.get("status", "unknown")
    result["reported_publication_ready"] = audit.get("publication_ready", False)
    result["reported_scope"] = audit.get("scope", "unknown")
    result["error_count"] = len(audit.get("errors", []))
    result["fresh_simulations"] = audit.get("scope") == "fresh_simulation_and_static" and audit.get("simulations_requested") is True
    result["verified_task_count"] = sum(row.get("status") == "verified" for row in audit.get("tasks", []))
    expected_ids = {row["id"] for row in index["tasks"]}
    actual_ids = [row.get("id") for row in audit.get("tasks", [])]
    captured = audit.get("inputs_sha256", {})
    index_key = audit.get("index_path", "")
    reasons = []
    if not index_key or captured.get(index_key) != _digest(bundle / "index.json"):
        reasons.append("Verification report is not bound to this exact index hash.")
    # Bind every indexed public task and private witness by content, including
    # relocated bundles. Nothing in a reference file is decoded or published.
    original_base = Path(index_key).parent
    for entry in index["tasks"]:
        for field, hash_field in (("path", "sha256"), ("reference_path", "reference_sha256")):
            expected = entry.get(hash_field)
            if not expected or captured.get(str(original_base / entry.get(field, ""))) != expected:
                reasons.append("Verification report is missing an indexed task/reference content hash.")
                break
    if audit.get("task_count") != len(expected_ids) or set(actual_ids) != expected_ids or len(actual_ids) != len(expected_ids):
        reasons.append("Verification report does not cover exactly the indexed task IDs.")
    result["artifact_binding"] = "matched" if not reasons else "mismatched"
    result["binding_issues"] = sorted(set(reasons))
    if reasons:
        result["status"] = "stale_or_mismatched"
        return result, {}
    result["status"] = ("failed" if audit.get("status") != "verified" or audit.get("errors") else
                        "static_only" if not result["fresh_simulations"] else
                        "verified" if audit.get("publication_ready") is True and
                        result["verified_task_count"] == len(expected_ids) else "incomplete_fresh_verification")
    default = audit.get("public_default_audit", {})
    outcomes = default.get("task_outcomes", {})
    valid_failures = sum(outcome.get("status") == "ok" and outcome.get("success") is False
                         for task_id, outcome in outcomes.items() if task_id in expected_ids)
    result["public_default"] = {"status": default.get("status", "pending"),
                                "valid_failed_tasks": valid_failures,
                                "solved_task_count": len(default.get("tasks_solved", [])),
                                "unresolved_task_count": len(default.get("unresolved_tasks", []))}
    if result["public_default"]["status"] == "verified" and (set(outcomes) != expected_ids or valid_failures != len(expected_ids)):
        result["public_default"]["status"] = "inconsistent"
    diversity = audit.get("reference_diversity", {})
    if diversity:
        result["reference_diversity"] = {
            "status": "passed" if diversity.get("passed") is True else "failed",
            "unique_reference_count": audit.get("unique_reference_count"),
            "computed_requirement_group_count": audit.get("computed_requirement_group_count"),
            "metric": diversity.get("metric"), "minimum_required_distance": diversity.get("minimum_required_distance"),
            "violation_count": len(diversity.get("violations", [])),
            "topologies": {name: {"reference_count": row.get("reference_count"),
                                  "minimum_observed_distance": row.get("minimum_observed_distance")}
                           for name, row in sorted(diversity.get("topologies", {}).items())}}
    coverage = audit.get("reference_solution_coverage", {})
    if "topologies" in coverage:
        result["reference_solution_coverage"] = {
            "status": "measured", "scope": coverage.get("scope"),
            "topologies": {name: {"task_count": row.get("task_count"),
                                  "greedy_cover_count": row.get("greedy_cover_count"),
                                  "maximum_tasks_solved_by_one_reference": row.get("maximum_tasks_solved_by_one_reference"),
                                  "uncovered_task_count": len(row.get("uncovered_task_ids", [])),
                                  "unresolved_reference_task_pairs": sum(len(item.get("unresolved_tasks", [])) for item in row.get("reference_coverage", [])),
                                  "ambiguous_reference_task_pairs": sum(len(item.get("ambiguous_tasks", [])) for item in row.get("reference_coverage", []))}
                           for name, row in sorted(coverage["topologies"].items())}}
    return result, {row["id"]: row for row in audit.get("tasks", [])}


def build_summary(bundle, verification=None):
    """Return only documentation-safe fields; do not copy private provenance."""
    bundle = Path(bundle).resolve()
    index = _load(bundle / "index.json")
    entries = index.get("tasks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("The bundle index needs a nonempty tasks list.")
    ids = [row["id"] for row in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate task ID in bundle index.")
    warnings = []
    if index.get("task_count", len(entries)) != len(entries):
        warnings.append("Declared task_count disagrees with actual index entries.")
    if index.get("training_approved") is True or index.get("model_training_performed") is True:
        warnings.append("Index training claims conflict with this pending-approval reporting policy.")
    records, sources, family_splits, grouped = [], {}, defaultdict(set), defaultdict(list)
    measurement_profiles = Counter()
    methods = {method: {"completed_trials": 0, "per_task_trials": [],
                        "successes_at": {str(k): 0 for k in (5, 10, 30)}} for method in METHODS}
    label_mismatches = []
    curation_counts = {level: Counter() for level in LEVELS}
    for entry in entries:
        for field, hash_field in (("path", "sha256"), ("reference_path", "reference_sha256")):
            if not entry.get(field) or _digest(_contained(bundle, entry[field])) != entry.get(hash_field):
                raise ValueError("Indexed artifact content hash disagrees: " + entry["id"] + " / " + field)
        task = _load(_contained(bundle, entry["path"]))
        if task.get("id") != entry["id"]:
            raise ValueError("Task ID disagrees with bundle index: " + entry["id"])
        if entry.get("difficulty") not in LEVELS or entry.get("split") not in SPLITS:
            raise ValueError("Unsupported difficulty or split: " + entry["id"])
        if task.get("max_evaluations") != 30:
            warnings.append("Task does not use the declared 30-evaluation budget: " + entry["id"])
        source_path = entry["source_record"]
        if entry["topology"] not in sources:
            sources[entry["topology"]] = _source_summary(entry["topology"], source_path, _load(_contained(bundle, source_path)))
        elif sources[entry["topology"]]["record"] != source_path:
            raise ValueError("A topology has inconsistent source records.")
        stats = entry.get("baseline_summary", {})
        try:
            measured_level = observed_level(stats, minimum_trials=10, hard_trials=20)
        except (KeyError, TypeError, ValueError):
            measured_level = "incomplete"
        if measured_level != entry["difficulty"]:
            label_mismatches.append({"id": entry["id"], "declared": entry["difficulty"], "from_summary": measured_level})
        for method, summary in methods.items():
            method_stats = stats.get(method, {})
            trials = method_stats.get("completed_trials", 0)
            summary["completed_trials"] += trials
            summary["per_task_trials"].append(trials)
            for budget in summary["successes_at"]:
                summary["successes_at"][budget] += method_stats.get("success_at", {}).get(budget, {}).get("successes", 0)
        topology, family = entry["topology"], entry["topology_family"]
        changed_count = entry.get("substantive_changed_control_count")
        if type(changed_count) is int and changed_count >= 0 and type(entry.get("start_change_curation_passed")) is bool:
            policy_ok = (entry["difficulty"] == "easy" or entry["difficulty"] == "medium" and changed_count >= 2 or
                         entry["difficulty"] == "hard" and changed_count >= 3 and entry.get("start_variant") == "distant")
            curation_counts[entry["difficulty"]]["reported_pass" if entry["start_change_curation_passed"] and policy_ok else "reported_fail"] += 1
        else:
            curation_counts[entry["difficulty"]]["pending"] += 1
        family_splits[family].add(entry["split"])
        grouped[(topology, family)].append(entry)
        conditions, ac, transient = task["conditions"], task["ac"], task["transient"]
        step = abs(transient["high_v"] - transient["low_v"])
        measurement_profiles[("AutoCkt" if topology == "autockt_two_stage" else
                              "AnalogGym" if sources[topology]["repository"] == "https://github.com/CODA-Team/AnalogGym" else topology,
                              conditions["corner"], conditions["supply_v"], conditions["temperature_c"],
                              ac["start_hz"], ac["points_per_decade"], transient["settling_tolerance_v"],
                              step, transient["minimum_hold_s"])] += 1
        records.append({"id": entry["id"], "path": entry["path"], "topology": topology,
                        "topology_family": family, "difficulty": entry["difficulty"], "split": entry["split"],
                        "parameter_count": len(task["parameters"]),
                        "initial_failed_requirements": list(entry.get("initial_failed_requirements", [])),
                        "starting_failure_evidence": "calibration_index",
                        "calibration_label_consistent": measured_level == entry["difficulty"]})
    for summary in methods.values():
        trials = summary.pop("per_task_trials")
        summary["minimum_trials_per_task"] = min(trials)
        summary["maximum_trials_per_task"] = max(trials)
        summary["success_at"] = {budget: {"successes": count, "trials": summary["completed_trials"],
                                          "rate": count / summary["completed_trials"] if summary["completed_trials"] else None}
                                 for budget, count in summary.pop("successes_at").items()}
    audit, audited_tasks = _audit_summary(bundle, index, verification)
    for record in records:
        initial = audited_tasks.get(record["id"], {}).get("evaluations", {}).get("initial", {})
        if initial.get("status") == "ok" and initial.get("success") is False:
            failed = sorted(name for name, passed in initial.get("checks", {}).items() if passed is False)
            if failed:
                if set(failed) != set(record["initial_failed_requirements"]):
                    warnings.append("Fresh initial failure checks disagree with calibration index: " + record["id"])
                record["initial_failed_requirements"] = failed
                record["starting_failure_evidence"] = "fresh_verification"
    split_violations = sorted(family for family, values in family_splits.items() if len(values) != 1)
    if split_violations:
        warnings.append("Topology families span multiple splits.")
    if label_mismatches:
        warnings.append("Some difficulty labels lack matching minimum-trial calibration summaries.")
    pilot = _pilot_exclusion(bundle, entries)
    if pilot["status"] == "overlap":
        warnings.append("Historical pilot task IDs or exact file contents overlap this bundle.")
    verified = (audit["status"] == "verified" and audit["public_default"]["status"] == "verified" and
                audit["reference_diversity"]["status"] == "passed" and not warnings)
    return {"schema_version": 1, "generated_utc": datetime.now(timezone.utc).isoformat(),
            "collection": index.get("collection", bundle.name), "index_sha256": _digest(bundle / "index.json"),
            "index_status": index.get("status", "not_declared"),
            "release_status": "automated_verification_passed" if verified else "benchmark_candidates_pending_validation",
            "task_count": len(entries), "topology_count": len(sources), "family_count": len(family_splits),
            "difficulty_counts": _counts(entries, "difficulty", LEVELS), "split_counts": _counts(entries, "split", SPLITS),
            "requirement_group_count": len({row["requirement_group"] for row in entries}),
            "declared_distinct_reference_hashes": len({(row["topology"], row["reference_sha256"]) for row in entries}),
            "training_approved": False, "model_training_performed": False,
            "independent_expert_review": "pending", "untrained_llm_evaluation": "not_performed",
            "split_policy": {"strategy": "whole_topology_family_holdout", "consistent": not split_violations,
                             "violating_families": split_violations,
                             "families": {name: {"splits": sorted(splits), "task_count": sum(len(rows) for (_, family), rows in grouped.items() if family == name)}
                                          for name, splits in sorted(family_splits.items())}},
            "topologies": [{"topology": name, "family": family, "task_count": len(rows),
                            "difficulty_counts": _counts(rows, "difficulty", LEVELS), "split_counts": _counts(rows, "split", SPLITS)}
                           for (name, family), rows in sorted(grouped.items())],
            "measurement_profiles": [{"source": key[0], "corner": key[1], "supply_v": key[2], "temperature_c": key[3],
                                      "gain_frequency_hz": key[4], "ac_points_per_decade": key[5],
                                      "settling_tolerance_v": key[6], "step_v": key[7],
                                      "settling_band_percent_of_step": 100 * key[6] / key[7] if key[7] else None,
                                      "minimum_hold_s": key[8], "task_count": count}
                                     for key, count in sorted(measurement_profiles.items())],
            "baselines": {"budget": 30, "method_count": len(METHODS), "methods": methods,
                          "method_descriptions": {method: BASELINE_DESCRIPTIONS.get(method, "Declared baseline; see its implementation for the exact search policy.") for method in METHODS},
                          "difficulty_summary_check": "consistent" if not label_mismatches else "inconsistent",
                          "label_mismatches": label_mismatches,
                          "evidence_scope": "Indexed confirmation summaries; this reporter does not replay searches or re-audit private trajectory contents."},
            "start_change_curation": {"status": "metadata_consistent" if all(not counts["pending"] and not counts["reported_fail"] for counts in curation_counts.values()) else "pending_or_inconsistent",
                                      "per_difficulty": {level: {key: counts[key] for key in ("reported_pass", "reported_fail", "pending")}
                                                         for level, counts in curation_counts.items()},
                                      "evidence_scope": "Trusted index flags/counts only. This reporter does not inspect private reference values or disclose which controls differ."},
            "historical_pilot_exclusion": pilot, "verification": audit,
            "sources": [sources[name] for name in sorted(sources)],
            "tasks": sorted(records, key=lambda row: row["id"]), "warnings": sorted(set(warnings))}


def _format_number(value):
    if value is None:
        return "pending / not applicable"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite report value.")
        return f"{value:.6g}"
    return str(value)


def render_readme(summary, bundle):
    bundle = Path(bundle).resolve()
    try:
        command_bundle = str(bundle.relative_to(ROOT))
    except ValueError:
        command_bundle = str(bundle)
    index_argument = shlex.quote(command_bundle + "/index.json")
    export_argument = command_bundle + "_learner"
    verified = summary["release_status"] == "automated_verification_passed"
    audit = summary["verification"]
    lines = ["# " + _cell(summary["collection"]), "",
             f"This bundle contains **{summary['task_count']} new benchmark {'tasks' if verified else 'task candidates'}** across "
             f"{summary['topology_count']} amplifier topologies and {summary['family_count']} topology families. "
             + ("The linked automated release verification passed for these exact indexed artifacts. " if verified else
                "It is awaiting complete release validation; the candidate count is not a claim of a validated release. ") +
             "The project target is 250 tasks. Sizes and numerical targets were curated using simulations of paper-informed SKY130 adaptations; they are not recovered measurements from the papers.", "",
             "**Training approval: false. Independent expert review: pending. Model training: not performed. "
             "Untrained-LLM difficulty: not measured.** Automated simulator checks and baseline calibration do not provide expert certification or establish that a trained model will be best.", "",
             "The full bundle is evaluator-side material. " + _link("Task catalog", "TASK_CATALOG.md") +
             " lists every public problem and its starting failed requirements; " + _link("compact audit", "audit_summary.json") +
             " contains the generated counts. Keep the private reference solutions and calibration trajectories in " +
             _link("private/", "private/") + " away from the learner, and use the safe export below. This card does not reproduce solution vectors or trajectories.", "",
             "## Composition and split", "",
             _table(["Difficulty", "Tasks"], [(level, summary["difficulty_counts"][level]) for level in LEVELS]), "",
             _table(["Split", "Tasks"], [(split, summary["split_counts"][split]) for split in SPLITS]), "",
             "The split holds out whole topology families. Actual family assignment is shown below; "
             f"family separation check: **{'passed' if summary['split_policy']['consistent'] else 'FAILED'}**. "
             "This tests transfer across the listed circuit families under shared simulator, process models, and metric definitions. "
             "It does not establish generalization to unseen fabrication processes, circuit classes, or measurements.", "",
             _table(["Topology", "Family", "Train", "Validation", "Test", "Easy", "Medium", "Hard", "Total"],
                    [(row["topology"], row["family"], *(row["split_counts"][split] for split in SPLITS),
                      *(row["difficulty_counts"][level] for level in LEVELS), row["task_count"]) for row in summary["topologies"]]), "",
             f"The index contains {summary['requirement_group_count']} requirement groups and "
             f"{summary['declared_distinct_reference_hashes']} distinct topology/reference-file-hash pairs. "
             "Reference-vector uniqueness and geometric separation are separate verifier checks reported below.", "",
             "## Task and measurement contract", "",
             "An episode edits only the declared parameters within their bounds and must meet every fixed constraint. "
             "The budget is 30 logical evaluations, including the initial submission and invalid simulations. "
             "The baselines skip duplicate proposals before evaluation, consuming no call for those proposals. Cache reuse saves physical simulator work without "
             "increasing the logical budget. Tests use nominal transistor-level ngspice simulations with fixed SKY130 models; "
             "actual supply, temperature, corner, AC sampling, and settling definitions are tabulated here.", "",
             _table(["Source", "Tasks", "Corner", "Supply (V)", "Temperature (°C)", "Gain frequency (Hz)",
                     "AC points/decade", "Step (V)", "Settling band (V)", "Band / step", "Minimum hold (s)"],
                    [(row["source"], row["task_count"], row["corner"], row["supply_v"], row["temperature_c"],
                      row["gain_frequency_hz"], row["ac_points_per_decade"], _format_number(row["step_v"]),
                      row["settling_tolerance_v"], _format_number(row["settling_band_percent_of_step"]) + "%",
                      _format_number(row["minimum_hold_s"])) for row in summary["measurement_profiles"]]), "",
             _table(["Metric", "Exact meaning"], [
                 ("gain_db", "20 log10 of the open-loop AC gain magnitude at the first sampled frequency (0.1 Hz in the current task templates), not a fitted DC gain."),
                 ("unity_gain_hz", "The unique downward 0 dB crossing, interpolated in log frequency. Missing, upward-only, or multiple unity crossings invalidate the AC result."),
                 ("phase_margin_deg", "180° plus unwrapped open-loop phase at that unity crossing. The stability floor is 60°; it is not relaxed to fill the task quota."),
                 ("power_w", "Quiescent supply power: −I(VDD) × supply voltage from the DC operating point. This is not average transient or dynamic power."),
                 ("dc_error_v", "Absolute follower DC output error relative to the declared common-mode input."),
                 ("max_tracking_error_v", "Maximum absolute output–input error before the rising edge and in the final quarter of each plateau; edge transients are excluded. This is a separate constraint from the settling band."),
                 ("settling_rise_s / settling_fall_s", "Time from the relevant edge start to the first sampled point after the last excursion outside the absolute settling band, with the required remaining hold interval inside the finite observation window.")]), "",
             "AnalogGym tasks use a 20 mV settling band for a 200 mV step (10%); AutoCkt tasks use a 2 mV band "
             "for a 100 mV step (2%). These settling metrics are not interchangeable. Pass/fail agreement is required "
             "between the waveform-based extractor and independent ngspice native measurements. Fresh release verification "
             "also doubles AC sampling density and halves the maximum transient step for the reference checks. "
             "No layout, fabrication, noise, PSRR, CMRR, mismatch, or process-corner robustness claim is made.", "",
             "## Observed difficulty", "",
             f"Difficulty is relative to {summary['baselines']['method_count']} declared search baselines: " +
             ", ".join("`" + method + "`" for method in summary["baselines"]["methods"]) + ". "
             "Every method begins from the same task start and has the same 30-evaluation budget.", "",
             _table(["Method", "Search policy"], [(method, summary["baselines"]["method_descriptions"][method])
                                                  for method in summary["baselines"]["methods"]]), "",
             "Labels require at least 10 confirmation seeds per method. **Easy** means at least one method succeeds "
             "in ≥80% of its trials by call 30 and has median calls ≤10 when failed trials are censored at 30. "
             "**Hard** means every method succeeds in ≤20% of trials by call 30, with at least 20 trials per method. "
             "**Medium** covers the remaining confirmed tasks. A possible hard task with fewer than 20 trials is "
             "insufficiently confirmed. Labels are baseline-relative observations, not absolute mathematical or human difficulty.", "",
             "The final frontier selector also applies conservative start-design curation filters after measured "
             "classification: medium candidates must differ substantively from their private reference in at least "
             "two controls; hard candidates must use the distant-start variant and differ in at least three. "
             "A continuous-control change counts at normalized distance ≥0.025 in log coordinates for positive bounds "
             "or linear coordinates otherwise; integer controls require a change of at least one. Easy candidates "
             "have no additional control-count filter. These are exclusion rules, never replacement difficulty labels "
             "or proofs of absolute hardness. Which controls differ remains private.", "",
             "Reported curation metadata status: **" + summary["start_change_curation"]["status"] + "**. "
             "Counts below come from the trusted index; this reporting pass does not inspect private reference values.", "",
             _table(["Measured difficulty", "Reported pass", "Reported fail", "Pending metadata"],
                    [(level, *(summary["start_change_curation"]["per_difficulty"][level][key]
                               for key in ("reported_pass", "reported_fail", "pending"))) for level in LEVELS]), "",
             _table(["Method", "Confirmation trials", "Seeds per task: min–max", "Success@5", "Success@10", "Success@30"],
                    [(method, stats["completed_trials"], f"{stats['minimum_trials_per_task']}–{stats['maximum_trials_per_task']}",
                      *(f"{stats['success_at'][str(k)]['successes']}/{stats['success_at'][str(k)]['trials']}" for k in (5, 10, 30)))
                     for method, stats in summary["baselines"]["methods"].items()]), "",
             "The table pools reported confirmation outcomes, not independent task draws. Per-task calibration evidence "
             "records success@5/10/30, successful-trial and failure-censored medians, and 95% Wilson intervals. "
             "Small seed counts produce wide intervals; 0/20 is not evidence of a zero success probability. "
             "The intended protocol uses separate screening and confirmation runs; this reporter does not independently "
             "establish their seed disjointness. Labels and the selected collection depend on observed outcomes. "
             "Shared families, shared starts, and deterministic measurement reuse further limit "
             "population-level interpretation. No LLM baseline or RL training result is included. "
             f"Indexed label/summary consistency: **{summary['baselines']['difficulty_summary_check']}**. "
             "This reporting pass does not independently replay or inspect private baseline trajectories.", "",
             "## Exclusion and independent verification", ""]
    pilot = summary["historical_pilot_exclusion"]
    if pilot["status"] != "pending":
        lines.extend([f"Historical pilot exclusion: **{pilot['status']}**. Comparing against the " +
                      _link(f"{pilot['pilot_task_count']}-task pilot index", pilot["pilot_index"]) +
                      f" found {pilot['task_id_overlap_count']} task-ID overlaps and {pilot['task_sha256_overlap_count']} "
                      "serialized-task-hash overlaps. This proves exact artifact exclusion only. Paper and topology reuse "
                      "is intentional; broader semantic duplication and unknown pretraining exposure are not assessed.", ""])
    else:
        lines.extend(["Historical pilot artifact exclusion: **pending**; the pilot index was unavailable.", ""])
    default = audit["public_default"]
    lines.extend(["Release assembly requires each rounded start to simulate successfully and fail at least one requirement, "
                  "each private reference to pass all requirements with the prescribed margins, and the current public "
                  "catalog seed to simulate successfully while failing the task. A fresh public-default replay then "
                  "checks the default against every new target set. A failed simulator run is unresolved evidence, not "
                  "proof that the seed cannot solve the task. Historical pilot answer vectors are not separately certified "
                  "by an ID/hash exclusion check.", "",
                  f"Fresh public-default audit: **{default['status']}**; "
                  f"valid failed tasks: {default.get('valid_failed_tasks', 'pending')}; "
                  f"tasks solved: {default.get('solved_task_count', 'pending')}; "
                  f"unresolved tasks: {default.get('unresolved_task_count', 'pending')}.", "",
                  f"Automated verification status: **{audit['status']}**; artifact binding: **{audit['artifact_binding']}**; "
                  f"freshly verified tasks: {audit['verified_task_count']}/{summary['task_count']}.", ""])
    if audit["report"]:
        lines.extend(["Evidence: " + _link("independent verification report (evaluator-only)", audit["report"]) +
                      ". This report may contain private design values and must stay outside the learner's accessible files.", ""])
    diversity = audit["reference_diversity"]
    coverage = audit["reference_solution_coverage"]
    if diversity["status"] != "pending":
        lines.extend([f"Reference diversity check: **{diversity['status']}**; unique vectors: "
                      f"{diversity.get('unique_reference_count')}; minimum required normalized distance: "
                      f"{_format_number(diversity.get('minimum_required_distance'))}. "
                      "Distance is RMS separation in normalized log coordinates for positive-bounded parameters "
                      "and linear coordinates otherwise, omitting fixed dimensions. The per-topology minimum is "
                      "the smallest nearest-reference distance.", "",
                      _table(["Topology", "References", "Minimum nearest distance", "Greedy cover size", "Most tasks solved by one reference", "Uncovered tasks"],
                             [(name, row["reference_count"], _format_number(row["minimum_observed_distance"]),
                               coverage.get("topologies", {}).get(name, {}).get("greedy_cover_count", "pending"),
                               coverage.get("topologies", {}).get(name, {}).get("maximum_tasks_solved_by_one_reference", "pending"),
                               coverage.get("topologies", {}).get(name, {}).get("uncovered_task_count", "pending"))
                              for name, row in diversity.get("topologies", {}).items()]), ""])
    else:
        lines.extend(["Reference-vector uniqueness, nearest-reference distances, and solution-coverage measurements: **pending**.", ""])
    lines.extend([f"Reference solution-coverage status: **{coverage['status']}**. When measured, the greedy cover uses "
                  "only this finite collection of verified private references under identical circuit and measurement "
                  "conditions. It is not an optimal set cover or a universal hardness measure. Reference separation "
                  "alone does not prevent one solution from meeting several tasks; the overlap counts expose that limitation.", ""])
    if summary["warnings"]:
        lines.extend(["Outstanding reporting issues:", "", *("- " + _cell(warning) for warning in summary["warnings"]), ""])
    lines.extend(["## Source provenance and reading limits", "",
                  "Each topology has a bundled source record. The table preserves the recorded reading level; "
                  "an inspected released schematic or abstract does not establish that an original full paper was read. "
                  "The published circuits were adapted to the repository's SKY130 process and project-authored testbenches. "
                  "Numerical target requirements come from simulation-guided curation, not paper-reproduction claims.", "",
                  _table(["Topology", "Paper", "Recorded review level", "Source record"],
                         [(source["topology"], _link(source["title"], source["url"]) if source["url"] else source["title"],
                           source["review_level"].replace("_", " "), _link("record", source["record"])) for source in summary["sources"]]), ""])
    for source in summary["sources"]:
        lines.extend(["**" + _cell(source["topology"]) + "**. " + _cell(source["claim_level"]).replace("_", " "), ""])
        if source["repository"]:
            lines.extend(["Released implementation: " + _link("repository", source["repository"]) +
                          "; pinned commit `" + str(source["commit"]) + "`. " + _cell(source["license"]), ""])
        details = source["adaptations"] + source["review_limitations"]
        if details:
            lines.extend([*("- " + _cell(detail) for detail in details), ""])
    lines.extend(["Retain the upstream notices when redistributing derivative netlists. Bundled source records may "
                  "contain historical parameter values, so the learner export intentionally omits them.", "",
                  "## Reproduction and safe learner export", "",
                  "Run these commands from the repository root in the configured ngspice/SKY130 environment. "
                  "Fresh verification creates a new run directory and evaluates starts, references, refined references, "
                  "and public defaults. Static verification without `--simulate` is insufficient for release validation.", "",
                  "```sh", "python3 scripts/verify_benchmark.py \\", "  --index " + index_argument + " \\",
                  f"  --expected-count {summary['task_count']} --simulate --workers 4 \\",
                  "  --simulator-timeout 30 --minimum-reference-distance 0.025", "```", "",
                  "Regenerate this card with the new verification report (replace the example run path):", "",
                  "```sh", "python3 -m benchmark.report --bundle " + shlex.quote(command_bundle) + " \\",
                  "  --verification runs/NEW_VERIFICATION_RUN/verification.json", "```", "",
                  "Export public specifications into a directory that does not already exist:", "",
                  "```sh", "python3 - <<'PY'", "from benchmark.learner import export_learner_bundle",
                  "export_learner_bundle(" + repr(command_bundle + "/index.json") + ", " + repr(export_argument) + ")",
                  "PY", "```", "",
                  "The export contains learner specifications, not executable simulator tasks. It omits private "
                  "solutions, provenance, calibration files, and source records. Keep the original bundle and simulator "
                  "in an evaluator process/filesystem inaccessible to the learner; `BenchmarkEpisode` and the exporter "
                  "do not themselves provide operating-system isolation. The evaluator can instantiate "
                  "`BenchmarkEpisode(task_path, output_directory, for_training=True)` and exchange only its sanitized "
                  "`specification()`, parameter actions, and `step(action)` observations with a learner. Training mode "
                  "remains subject to the existing qualification gate. Exporting files grants no training approval.", ""])
    return "\n".join(lines)


def render_catalog(summary):
    rows = [( _link(row["id"], row["path"]), row["topology"], row["topology_family"], row["split"], row["difficulty"],
              row["parameter_count"], ", ".join(row["initial_failed_requirements"]) or "unrecorded / pending",
              row["starting_failure_evidence"]) for row in summary["tasks"]]
    return "\n".join(["# Task catalog", "",
                      f"{summary['task_count']} indexed tasks. Status: **{summary['release_status']}**. "
                      "Training approval is false; independent expert review is pending. "
                      "Links open public problem JSON files with requirements and allowed parameter domains, "
                      "not reference solutions. Initial failed requirements come from fresh verification when available, "
                      "otherwise the indexed calibration record.", "",
                      _table(["Task", "Topology", "Family", "Split", "Observed difficulty", "Editable parameters",
                              "Starting failed requirements", "Evidence"], rows), ""])


def write_report(bundle, verification=None):
    """Regenerate only README.md, TASK_CATALOG.md, and audit_summary.json."""
    bundle = Path(bundle).resolve()
    summary = build_summary(bundle, verification)
    readme, catalog = render_readme(summary, bundle), render_catalog(summary)
    if _digest(bundle / "index.json") != summary["index_sha256"]:
        raise ValueError("Bundle index changed during reporting.")
    (bundle / "README.md").write_text(readme)
    (bundle / "TASK_CATALOG.md").write_text(catalog)
    (bundle / "audit_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--verification", type=Path)
    args = parser.parse_args()
    summary = write_report(args.bundle, args.verification)
    print(json.dumps({key: summary[key] for key in ("release_status", "task_count", "difficulty_counts", "split_counts",
                                                   "training_approved", "independent_expert_review")}, indent=2))


if __name__ == "__main__":
    main()
