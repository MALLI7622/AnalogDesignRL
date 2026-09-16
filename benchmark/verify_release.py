"""Independently check a release's files, contracts, and fresh circuit simulations.

Generator success fields and cached measurement results are never used. Static
verification alone cannot qualify a release for publication or approve training.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from analog_design.crosscheck import compare
from analog_design.metrics import score
from analog_design.model_clients import strict_json
from analog_design.qualification import require_training_approval
from analog_design.simulator import ROOT, digest, evaluate


METRIC_DIRECTIONS = {"gain_db": "min", "unity_gain_hz": "min", "phase_margin_deg": "min",
                     "power_w": "max", "dc_error_v": "max", "max_tracking_error_v": "max",
                     "settling_rise_s": "max", "settling_fall_s": "max"}
IDENTITY_FIELDS = ("circuit_directory", "subcircuit", "conditions", "parameters", "ac", "transient")


def _save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _number(value, name, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or (positive and value <= 0)):
        raise ValueError(f"Invalid finite numeric value: {name}")
    return value


def _hash(value):
    def normalize(item):
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            # Compare the numbers ngspice actually receives, including 1/1.0.
            number = float(f"{item:.15g}")
            return int(number) if number.is_integer() else number
        return item
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _contained(base, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Artifact paths must be relative strings.")
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError(f"Artifact path escapes its root: {relative}")
    return path


def _parameters(task, values, label):
    rules = task["parameters"]
    if not isinstance(rules, dict) or not rules or not isinstance(values, dict) or set(values) != set(rules):
        raise ValueError(f"{label} must contain exactly every editable parameter.")
    for name, rule in rules.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid editable parameter name.")
        low = _number(rule["min"], name + ".min")
        high = _number(rule["max"], name + ".max")
        value = _number(values[name], label + "." + name)
        if low > high or not low <= value <= high:
            raise ValueError(f"{label} parameter is outside valid bounds: {name}")
        if not isinstance(rule.get("unit"), str) or not rule["unit"].strip():
            raise ValueError(f"Parameter unit is missing: {name}")
        if "integer" in rule and type(rule["integer"]) is not bool:
            raise ValueError(f"Integer rule must be Boolean: {name}")
        if rule.get("integer") and value != int(value):
            raise ValueError(f"{label} parameter must be integral: {name}")


def _task_contract(task):
    if type(task.get("max_evaluations")) is not int or task["max_evaluations"] != 30:
        raise ValueError("Every benchmark task must have a 30-evaluation budget.")
    constraints = task["constraints"]
    if not isinstance(constraints, dict) or set(constraints) != set(METRIC_DIRECTIONS):
        raise ValueError("The task must declare exactly all eight required metrics.")
    for metric, direction in METRIC_DIRECTIONS.items():
        if not isinstance(constraints[metric], dict) or set(constraints[metric]) != {direction}:
            raise ValueError(f"Incorrect requirement direction: {metric}")
        _number(constraints[metric][direction], metric, positive=True)
    _parameters(task, task["initial_parameters"], "initial")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", task["subcircuit"]):
        raise ValueError("Invalid subcircuit name.")
    conditions = task["conditions"]
    if not re.fullmatch(r"[A-Za-z0-9_]+", conditions["corner"]):
        raise ValueError("Invalid model corner.")
    supply = _number(conditions["supply_v"], "supply", positive=True)
    _number(conditions["temperature_c"], "temperature")
    _number(conditions["load_f"], "load", positive=True)
    if not 0 < _number(conditions["common_mode_v"], "common mode") < supply:
        raise ValueError("Common mode must be inside the supply rails.")
    ac = task["ac"]
    if type(ac["points_per_decade"]) is not int or ac["points_per_decade"] < 100:
        raise ValueError("AC resolution must be an integer of at least 100 points per decade.")
    if not 0 < _number(ac["start_hz"], "AC start") < _number(ac["stop_hz"], "AC stop"):
        raise ValueError("Invalid AC frequency range.")
    transient = task["transient"]
    for name in ("rise_start_s", "edge_s", "high_duration_s", "period_s", "stop_s",
                 "max_step_s", "settling_tolerance_v", "minimum_hold_s"):
        _number(transient[name], name, positive=True)
    if not 0 <= _number(transient["low_v"], "low stimulus") < _number(transient["high_v"], "high stimulus") <= supply:
        raise ValueError("Stimulus must rise inside the supply rails.")
    fall = transient["rise_start_s"] + transient["edge_s"] + transient["high_duration_s"]
    if (fall + transient["edge_s"] + transient["minimum_hold_s"] >= transient["stop_s"] or
            transient["rise_start_s"] + transient["period_s"] <= transient["stop_s"]):
        raise ValueError("Transient windows must include settling and exclude a second rising edge.")


def _source_identity(task, entry, domain):
    if entry["topology_family"] != domain["family"]:
        raise ValueError("Topology family disagrees with the trusted domain.")
    trusted = domain["task"]
    for field in IDENTITY_FIELDS:
        if _hash(task[field]) != _hash(trusted[field]):
            raise ValueError(f"Task {field} disagrees with the trusted circuit domain.")
    circuit = _contained(ROOT, task["circuit_directory"])
    source_path = circuit / "source.json"
    source = strict_json(source_path.read_text())
    if source != domain["source"]:
        raise ValueError("Circuit source record disagrees with the trusted domain.")
    netlist, parameters = circuit / "netlist.spice", circuit / "reference.params"
    source_files = source.get("files", {})
    expected_netlist = (source.get("release", {}).get("netlist_sha256") or
                        source.get("reference_design", {}).get("netlist_sha256") or
                        source_files.get("netlist.spice", {}).get("sha256"))
    expected_parameters = (source.get("release", {}).get("parameters_sha256") or
                           source.get("reference_design", {}).get("parameters_sha256") or
                           source_files.get("reference.params", {}).get("sha256"))
    if expected_netlist is None or expected_parameters is None:
        raise ValueError("Source record must pin the netlist and fixed parameter file hashes.")
    if digest(netlist) != expected_netlist or digest(parameters) != expected_parameters:
        raise ValueError("Circuit files disagree with pinned source hashes.")
    context = task.get("design_context")
    if context is not None:
        if not isinstance(context, dict) or context.get("topology") != entry["topology"]:
            raise ValueError("Public design context has a mismatched topology.")
        family = context.get("topology_family", context.get("family"))
        if family != entry["topology_family"]:
            raise ValueError("Public design context has a mismatched topology family.")
        if context.get("netlist") != netlist.read_text():
            raise ValueError("Public netlist differs from the evaluated circuit.")
        fixed = context.get("fixed_parameters")
        if not isinstance(fixed, dict) or set(fixed) & set(task["parameters"]):
            raise ValueError("Public fixed parameters expose an editable parameter or are malformed.")
        for name, value in fixed.items():
            _number(value, "fixed parameter " + name)
        trusted_context = trusted.get("design_context", {})
        for field in ("paper", "fixed_parameters"):
            if context.get(field) != trusted_context.get(field):
                raise ValueError(f"Public {field} disagrees with the trusted domain.")
        if not isinstance(context.get("scope"), str) or not context["scope"].strip():
            raise ValueError("Public context must state its scope.")
    return {str(path): digest(path) for path in (source_path, netlist, parameters)}


def _check_measurement(result, task, expected_success):
    if result.get("status") != "ok":
        raise ValueError("Fresh evaluation did not produce valid measurements: " + str(result.get("error", result.get("status"))))
    for name in ("metrics", "independent_metrics"):
        for metric in METRIC_DIRECTIONS:
            _number(result[name][metric], name + "." + metric)
    comparison = compare(result["metrics"], {key: result["independent_metrics"][key]
                                            for key in METRIC_DIRECTIONS}, task["transient"]["max_step_s"])
    primary = score(result["metrics"], task["constraints"])
    native = score(result["independent_metrics"], task["constraints"])
    if (not comparison["agrees"] or primary["checks"] != native["checks"] or
            result.get("checks") != primary["checks"]):
        raise ValueError("Fresh Python/native measurements or per-constraint decisions disagree.")
    if primary["success"] is not expected_success or result.get("success") is not expected_success:
        raise ValueError("Fresh evaluation has the wrong expected pass/fail outcome.")
    if not math.isclose(_number(result.get("reward"), "reward"), primary["reward"], rel_tol=0, abs_tol=1e-12):
        raise ValueError("Fresh reward disagrees with independently rescored measurements.")
    return {"status": result["status"], "success": primary["success"], "reward": primary["reward"],
            "metrics": result["metrics"], "independent_metrics": result["independent_metrics"],
            "checks": primary["checks"], "crosscheck": comparison,
            "simulator_invocations": result.get("simulator_invocations", 0)}


def _simulate_record(record, output, timeout):
    task, reference = record["task"], record["reference"]
    directory = output / "tasks" / record["id"]
    directory.mkdir(parents=True, exist_ok=False)
    task_path = directory / "task.json"
    _save(task_path, task)
    result = {"id": record["id"], "status": "failed", "errors": [], "evaluations": {}}
    variants = [("initial", task_path, {}, task, False),
                ("reference", task_path, reference, task, True)]
    refined = deepcopy(task)
    refined["ac"]["points_per_decade"] *= 2
    refined["transient"]["max_step_s"] /= 2
    refined_path = directory / "refined_task.json"
    _save(refined_path, refined)
    variants.append(("refined_reference", refined_path, reference, refined, True))
    for name, path, values, definition, expected in variants:
        try:
            measured = evaluate(path, values, directory / name, timeout)
            # Preserve an independent copy even if an evaluator implementation
            # fails to write its usual result artifact.
            _save(directory / (name + "_returned_result.json"), measured)
            result["evaluations"][name] = _check_measurement(measured, definition, expected)
        except Exception as error:
            result["errors"].append({"evaluation": name, "error": str(error), "type": type(error).__name__})
    if "reference" in result["evaluations"] and "refined_reference" in result["evaluations"]:
        coarse = result["evaluations"]["reference"]["metrics"]
        fine = result["evaluations"]["refined_reference"]["metrics"]
        result["refinement_absolute_changes"] = {key: abs(coarse[key] - fine[key]) for key in METRIC_DIRECTIONS}
        result["refinement_policy"] = "Original requirements must pass at both resolutions; metric changes are reported, not a claim of continuous-domain convergence."
    if not result["errors"]:
        result["status"] = "verified"
    _save(directory / "verification.json", result)
    return result


def _runtime_identity(task, timeout):
    # This records full model/executable identity; it never reads cached results.
    from benchmark.simulation import measurement_identity
    return measurement_identity(task, simulator_timeout=timeout)


def _reference_diversity(records, minimum_distance):
    groups = {}
    for record in records:
        groups.setdefault(record["entry"]["topology"], []).append(record)
    audit = {"metric": "RMS difference in normalized log coordinates for positive-bounded parameters; linear coordinates otherwise; fixed dimensions omitted",
             "minimum_required_distance": minimum_distance, "topologies": {}, "violations": []}
    for topology, group in sorted(groups.items()):
        group.sort(key=lambda record: record["id"])
        nearest = {record["id"]: None for record in group}
        pairs = []
        for index, first in enumerate(group):
            for second in group[index + 1:]:
                differences = []
                for name, rule in first["task"]["parameters"].items():
                    low, high = rule["min"], rule["max"]
                    if low == high:
                        continue
                    a, b = first["reference"][name], second["reference"][name]
                    delta = ((math.log(a) - math.log(b)) / (math.log(high) - math.log(low))
                             if low > 0 else (a - b) / (high - low))
                    differences.append(delta * delta)
                distance = math.sqrt(sum(differences) / len(differences)) if differences else 0.0
                pair = {"first": first["id"], "second": second["id"], "distance": distance}
                pairs.append(pair)
                for source, other in ((first, second), (second, first)):
                    current = nearest[source["id"]]
                    if current is None or distance < current["distance"]:
                        nearest[source["id"]] = {"reference_id": other["id"], "distance": distance}
                if distance < minimum_distance:
                    audit["violations"].append({"topology": topology, **pair})
        audit["topologies"][topology] = {"reference_count": len(group),
            "minimum_observed_distance": min((pair["distance"] for pair in pairs), default=None),
            "nearest_references": nearest, "pairwise_distances": pairs}
    audit["passed"] = not audit["violations"]
    return audit


def _rescore_witness(measurement, task):
    if measurement is None or measurement.get("status") != "ok":
        return {"status": "unresolved", "success": None,
                "reason": "A valid fresh measurement was unavailable; this is not evidence of physical infeasibility."}
    primary = score(measurement["metrics"], task["constraints"])
    native = score(measurement["independent_metrics"], task["constraints"])
    if primary["checks"] != native["checks"]:
        return {"status": "ambiguous", "success": None,
                "primary_checks": primary["checks"], "native_checks": native["checks"]}
    return {"status": "ok", **primary, "native_checks": native["checks"]}


def _simulate_default(topology, domain, output, timeout):
    # Use a permissive target only to retain physical measurements independently
    # of domain floors. Every final task is rescored against its original targets.
    task = deepcopy(domain["task"])
    task["id"] = "public_default_" + re.sub(r"[^A-Za-z0-9_-]", "_", topology)
    parameters = deepcopy(domain["public_default"])
    _parameters(task, parameters, "public default")
    task["initial_parameters"] = parameters
    task["constraints"] = {"power_w": {"max": 1e6}}
    directory = output / "public_defaults" / task["id"]
    directory.mkdir(parents=True, exist_ok=False)
    task_path = directory / "task.json"
    _save(task_path, task)
    _save(directory / "parameters.json", parameters)
    result = {"topology": topology, "status": "unresolved", "parameters": parameters,
              "errors": [], "measurement": None}
    try:
        measured = evaluate(task_path, parameters, directory / "measurement", timeout)
        _save(directory / "returned_result.json", measured)
        expected = score(measured.get("metrics", {}), task["constraints"])["success"]
        result["measurement"] = _check_measurement(measured, task, expected)
        result["status"] = "verified"
    except Exception as error:
        result["errors"].append({"error": str(error), "type": type(error).__name__})
    _save(directory / "verification.json", result)
    return result


def _reference_coverage(records, results):
    by_id = {result["id"]: result for result in results}
    groups = {}
    for record in records:
        groups.setdefault(record["entry"]["topology"], []).append(record)
    audit = {"scope": "Observed solution overlap among this finite set of fresh private references at identical circuit/measurement conditions; not universal solver hardness or an optimal set cover.",
             "topologies": {}}
    for topology, group in sorted(groups.items()):
        group.sort(key=lambda item: item["id"])
        coverage, available = [], {}
        for witness in group:
            measurement = by_id.get(witness["id"], {}).get("evaluations", {}).get("reference")
            solved, ambiguous, unresolved = [], [], []
            for target in group:
                outcome = _rescore_witness(measurement, target["task"])
                if outcome["status"] == "ambiguous":
                    ambiguous.append(target["id"])
                elif outcome["status"] == "unresolved":
                    unresolved.append(target["id"])
                elif outcome["success"]:
                    solved.append(target["id"])
            coverage.append({"reference_task_id": witness["id"], "tasks_solved": solved,
                             "tasks_solved_count": len(solved), "ambiguous_tasks": ambiguous,
                             "unresolved_tasks": unresolved})
            available[witness["id"]] = set(solved)
        uncovered, selected = {record["id"] for record in group}, []
        while uncovered:
            best = min(available, key=lambda name: (-len(available[name] & uncovered), name))
            if not available[best] & uncovered:
                break
            selected.append(best)
            uncovered -= available[best]
        audit["topologies"][topology] = {"task_count": len(group), "reference_coverage": coverage,
            "greedy_cover_reference_ids": selected, "greedy_cover_count": len(selected),
            "uncovered_task_ids": sorted(uncovered),
            "maximum_tasks_solved_by_one_reference": max((len(value) for value in available.values()), default=0)}
    return audit


def verify_release(index_path, output_directory, *, expected_count=None, simulate=False,
                   workers=4, simulator_timeout=30, domains_path=None, progress=None,
                   minimum_reference_distance=0.025):
    """Write a new audit directory and return its verified/failed report.

    A structurally valid static run has status ``verified`` but explicitly leaves
    ``publication_ready`` false. Publication requires ``--simulate`` with all
    three fresh evaluations per task. Training approval is never created.
    """
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be an integer between 1 and 4.")
    if expected_count is not None and (type(expected_count) is not int or expected_count < 1):
        raise ValueError("expected_count must be a positive integer.")
    _number(simulator_timeout, "simulator timeout", positive=True)
    _number(minimum_reference_distance, "minimum reference distance")
    if minimum_reference_distance < 0:
        raise ValueError("minimum_reference_distance cannot be negative.")
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=False)
    index_path = Path(index_path).resolve()
    domains_path = Path(domains_path or ROOT / "benchmark/domains.json").resolve()
    report = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
              "status": "failed", "scope": "fresh_simulation_and_static" if simulate else "static_only",
              "index_path": str(index_path), "domains_path": str(domains_path),
              "expected_count": expected_count, "task_count": 0, "errors": [], "tasks": [],
              "simulations_requested": bool(simulate), "publication_ready": False,
              "training_approved": False, "difficulty_validated": False,
              "difficulty_scope": "Labels are checked for schema only; search calibration is a separate audit.",
              "public_default_audit": {"status": "not_run"},
              "reference_solution_coverage": {"status": "not_run"},
              "workers": workers, "simulator_timeout_s": simulator_timeout}
    captured = {}
    records = []
    try:
        index = strict_json(index_path.read_text())
        domains = strict_json(domains_path.read_text())
        captured.update({str(index_path): digest(index_path), str(domains_path): digest(domains_path)})
        verifier_root = Path(__file__).resolve().parents[1]
        for relative in ("benchmark/verify_release.py", "scripts/verify_benchmark.py",
                         "analog_design/simulator.py", "analog_design/metrics.py", "analog_design/crosscheck.py"):
            path = verifier_root / relative
            captured[str(path)] = digest(path)
        if index.get("schema_version", 1) != 1:
            raise ValueError("Unsupported release index schema version.")
        domain_map = {entry["name"]: entry for entry in domains["entries"]}
        entries = index["tasks"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("The release index must contain a nonempty task list.")
        report["task_count"] = len(entries)
        if expected_count is not None and len(entries) != expected_count:
            report["errors"].append(f"Expected {expected_count} tasks, found {len(entries)}.")
        if "task_count" in index and index["task_count"] != len(entries):
            report["errors"].append("Declared task_count disagrees with the index entries.")
        ids, references, declared_groups, exact_groups, fingerprints, families = set(), {}, {}, {}, set(), {}
        for position, entry in enumerate(entries):
            label = str(entry.get("id", position)) if isinstance(entry, dict) else str(position)
            try:
                task_id = entry["id"]
                if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id):
                    raise ValueError("Invalid task ID.")
                if task_id in ids:
                    raise ValueError("Duplicate task ID.")
                ids.add(task_id)
                if entry["path"] != f"tasks/{task_id}.json" or entry["reference_path"] != f"private/{task_id}/reference.json":
                    raise ValueError("Task and reference paths do not match the release layout.")
                task_path = _contained(index_path.parent, entry["path"])
                reference_path = _contained(index_path.parent, entry["reference_path"])
                for path, field in ((task_path, "sha256"), (reference_path, "reference_sha256")):
                    if not re.fullmatch(r"[0-9a-f]{64}", entry[field]) or digest(path) != entry[field]:
                        raise ValueError(f"Artifact content hash disagrees: {field}")
                    captured[str(path)] = entry[field]
                task, reference = strict_json(task_path.read_text()), strict_json(reference_path.read_text())
                if task["id"] != task_id:
                    raise ValueError("Task ID disagrees with its index entry.")
                if entry["difficulty"] not in {"easy", "medium", "hard"} or entry["split"] not in {"train", "validation", "test"}:
                    raise ValueError("Invalid difficulty or dataset split.")
                _task_contract(task)
                _parameters(task, reference, "reference")
                if _hash(reference) == _hash(task["initial_parameters"]):
                    raise ValueError("Starting and reference vectors must differ.")
                domain = domain_map[entry["topology"]]
                captured.update(_source_identity(task, entry, domain))
                reference_key = (entry["topology"], _hash(reference))
                if reference_key in references:
                    raise ValueError("Duplicate reference vector within a topology: " + references[reference_key])
                references[reference_key] = task_id
                identity = {key: task[key] for key in (*IDENTITY_FIELDS, "constraints", "max_evaluations")}
                exact_group = _hash(identity)
                fingerprint = _hash({**identity, "initial_parameters": task["initial_parameters"]})
                if fingerprint in fingerprints:
                    raise ValueError("Duplicate task contents under different IDs.")
                fingerprints.add(fingerprint)
                declared = entry["requirement_group"]
                if not isinstance(declared, str) or not declared.strip():
                    raise ValueError("Missing requirement group.")
                for groups, group in ((declared_groups, declared), (exact_groups, exact_group)):
                    if group in groups and groups[group] != entry["split"]:
                        raise ValueError("Related requirements occur in different splits.")
                    groups[group] = entry["split"]
                family = entry["topology_family"]
                if index.get("split_policy") in {"topology", "topology_family"}:
                    if family in families and families[family] != entry["split"]:
                        raise ValueError("A topology family spans splits despite the declared policy.")
                    families[family] = entry["split"]
                if index.get("training_approved") is True or entry.get("training_approved") is True or task.get("training_approved") is True:
                    require_training_approval(task_path)
                records.append({"id": task_id, "task": task, "reference": reference,
                                "entry": entry, "requirement_identity": exact_group})
            except Exception as error:
                report["errors"].append({"task": label, "error": str(error), "type": type(error).__name__})
        report["reference_diversity"] = _reference_diversity(records, minimum_reference_distance)
        if not report["reference_diversity"]["passed"]:
            report["errors"].append({"error": "Reference vectors violate the minimum normalized distance.",
                                     "pairs": report["reference_diversity"]["violations"]})
        report["static_checks_passed"] = not report["errors"]
        report["unique_reference_count"] = len(references)
        report["computed_requirement_group_count"] = len(exact_groups)
        if report["static_checks_passed"] and simulate:
            runtime = {}
            for record in records:
                topology = record["entry"]["topology"]
                if topology not in runtime:
                    runtime[topology] = _runtime_identity(record["task"], simulator_timeout)
                    identity = runtime[topology]
                    captured.update(identity.get("spice_dependency_sha256", {}))
                    captured.update(identity.get("evaluator_sha256", {}))
                    binary = identity.get("runtime", {})
                    if binary.get("executable"):
                        captured[binary["executable"]] = binary["executable_sha256"]
            report["runtime_identities"] = runtime
            _save(output / "verification.json", report)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                defaults = {executor.submit(_simulate_default, topology, domain_map[topology], output,
                                            simulator_timeout): topology for topology in runtime}
                default_results = {}
                for future in as_completed(defaults):
                    topology = defaults[future]
                    try:
                        default_results[topology] = future.result()
                    except Exception as error:
                        default_results[topology] = {"topology": topology, "status": "unresolved",
                                                     "measurement": None, "errors": [str(error)]}
                default_outcomes = {}
                solved = []
                for record in records:
                    default = default_results[record["entry"]["topology"]]
                    outcome = _rescore_witness(default.get("measurement"), record["task"])
                    default_outcomes[record["id"]] = outcome
                    if outcome["success"] is True:
                        solved.append(record["id"])
                        report["errors"].append({"task": record["id"], "error": "The public default already solves this task."})
                unresolved = [task_id for task_id, outcome in default_outcomes.items() if outcome["status"] != "ok"]
                if unresolved:
                    report["errors"].append({"error": "Public-default replay is unresolved; valid failing measurements are required for release.",
                                             "tasks": unresolved})
                report["public_default_audit"] = {"status": "failed" if solved else "unresolved" if unresolved else "verified",
                    "topologies": default_results, "task_outcomes": default_outcomes,
                    "tasks_solved": solved, "unresolved_tasks": unresolved,
                    "policy": "Reject tasks solved by the public default. Invalid or ambiguous default measurements remain explicitly unresolved; they do not prove an infeasible default or task hardness."}
                _save(output / "verification.json", report)
                futures = {executor.submit(_simulate_record, record, output, simulator_timeout): record["id"]
                           for record in records}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as error:
                        result = {"id": futures[future], "status": "failed", "errors": [str(error)]}
                    report["tasks"].append(result)
                    if result["status"] != "verified":
                        report["errors"].append({"task": result["id"], "simulation_errors": result["errors"]})
                    _save(output / "verification.json", report)
                    if progress:
                        progress({"completed": len(report["tasks"]), "total": len(records),
                                  "id": result["id"], "status": result["status"]})
            report["reference_solution_coverage"] = _reference_coverage(records, report["tasks"])
        else:
            report["tasks"] = [{"id": record["id"], "status": "static_verified",
                                "requirement_identity": record["requirement_identity"]} for record in records]
        for path, expected in captured.items():
            if digest(path) != expected:
                report["errors"].append("Input changed during verification: " + path)
        report["status"] = "verified" if not report["errors"] else "failed"
        report["publication_ready"] = bool(simulate and report["status"] == "verified" and len(report["tasks"]) == len(entries))
    except Exception as error:
        report["errors"].append({"error": str(error), "type": type(error).__name__})
    finally:
        report["tasks"].sort(key=lambda item: item["id"])
        report["inputs_sha256"] = captured
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        _save(output / "verification.json", report)
    return report
