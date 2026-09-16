"""Assemble a benchmark release from independently calibrated task candidates."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from analog_design.metrics import score
from analog_design.crosscheck import compare
from analog_design.model_clients import strict_json
from analog_design.simulator import ROOT, digest, parameters_for
from benchmark.baselines import METHODS, run_search
from benchmark.calibrate import (BUDGET, CODE_FILES, PHYSICAL_FIELDS, admission_decision,
                                 load_checkpoint, summarize_trials)
from benchmark.candidates import requirement_identity
from benchmark.explore import rounded_parameters
from benchmark.frontier_select import admission_failures, start_curation
from benchmark.select import observed_level
from benchmark.simulation import canonical_hash, evaluator_hashes, include_closure, rescore, save_json
from benchmark.verify_release import METRIC_DIRECTIONS, _source_identity, _task_contract


SPLITS = {"two_stage_miller": "test", "peng_impedance_adapting": "test",
          "ramos_positive_feedback": "validation"}
REQUIRED_COUNTS = {"easy": 100, "medium": 125, "hard": 25}


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("Invalid artifact identifier.")
    return value


def _inside(directory, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Artifact paths must be relative.")
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Artifact path escapes its directory.")
    return path


class InputSnapshot:
    def __init__(self):
        self.hashes = {}

    def capture(self, path, expected=None):
        path = Path(path).resolve()
        actual = digest(path)
        prior = self.hashes.get(str(path), expected)
        if (prior is not None and prior != actual) or (expected is not None and expected != actual):
            raise ValueError("Input content hash disagrees or changed: " + str(path))
        self.hashes[str(path)] = actual
        return actual

    def read(self, path):
        self.capture(path)
        value = strict_json(Path(path).read_text())
        self.capture(path)
        return value

    def assert_unchanged(self):
        for filename, expected in self.hashes.items():
            if digest(filename) != expected:
                raise ValueError("Input changed before release publication: " + filename)


def candidate_records(directory, snapshot):
    directory = Path(directory).resolve()
    index = snapshot.read(directory / "index.json")
    if not isinstance(index.get("tasks"), list) or not index["tasks"]:
        raise ValueError("Candidate index must contain tasks.")
    result = {}
    for entry in index["tasks"]:
        name = _identifier(entry["id"])
        if name in result:
            raise ValueError("Duplicate candidate ID: " + name)
        if entry["path"] != f"candidates/{name}.json":
            raise ValueError("Candidate path must match its ID.")
        path = _inside(directory, entry["path"])
        result[name] = {**entry, "absolute_path": path}
    return result


def validate_evaluation(result, task):
    if result.get("status") != "ok":
        if result.get("success") is not False or result.get("reward") != -1:
            raise ValueError("An invalid simulation cannot succeed or receive another reward.")
        return
    for field in ("metrics", "independent_metrics"):
        for name in METRIC_DIRECTIONS:
            value = result[field][name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Nonfinite or missing calibration measurement.")
    primary = score(result["metrics"], task["constraints"])
    native = score(result["independent_metrics"], task["constraints"])
    agreement = compare(result["metrics"], {key: result["independent_metrics"][key] for key in METRIC_DIRECTIONS},
                        task["transient"]["max_step_s"])
    if not agreement["agrees"] or primary["checks"] != native["checks"]:
        raise ValueError("Calibration measurement paths disagree.")
    if any(result.get(key) != primary[key] for key in ("success", "checks", "reward")):
        raise ValueError("Calibration success, checks or reward disagree with the measurements.")


def validate_trial(trial, task, method, seed, check_measurement=None):
    """Replay the deterministic baseline against its recorded observations.

    This checks every proposed action, budget charge, outcome and stop decision;
    it does not launch ngspice or supply a solver with reference information.
    """
    if (trial.get("task_id") != task["id"] or trial.get("method") != method
            or type(trial.get("seed")) is not int or trial["seed"] != seed):
        raise ValueError("Trial task ID, method or seed disagrees with its identity.")
    trajectory = trial.get("trajectory")
    if not isinstance(trajectory, list) or not 1 <= len(trajectory) <= BUDGET:
        raise ValueError("A trial must contain one to thirty charged evaluations.")
    if (type(trial.get("max_evaluations")) is not int or trial["max_evaluations"] != BUDGET
            or type(trial.get("evaluations_used")) is not int or trial["evaluations_used"] != len(trajectory)
            or type(trial.get("success")) is not bool):
        raise ValueError("Trial budget, count or success has an invalid value or type.")
    cursor = 0

    def replay(parameters):
        nonlocal cursor
        if cursor >= len(trajectory):
            raise ValueError("Trial ended before the baseline budget or stopping condition.")
        step = trajectory[cursor]
        if type(step.get("evaluation")) is not int or step["evaluation"] != cursor + 1 or step.get("parameters") != parameters:
            raise ValueError("Recorded action or evaluation numbering disagrees with deterministic baseline replay.")
        validate_evaluation(step, task)
        if check_measurement:
            check_measurement(step, parameters)
        cursor += 1
        return step

    replayed = run_search(task, replay, method, seed, budget=BUDGET)
    if cursor != len(trajectory):
        raise ValueError("Trial contains evaluations after termination.")
    for key, value in replayed.items():
        if key == "trajectory":
            for expected, recorded in zip(value, trajectory):
                if any(recorded.get(field) != item for field, item in expected.items()):
                    raise ValueError("Trial observation disagrees with baseline replay.")
        elif trial.get(key) != value:
            raise ValueError("Trial summary disagrees with replay: " + key)
    return trial


class CalibrationEvidence:
    """Validate frozen calibration inputs, checkpoints, and physical evidence."""
    def __init__(self, candidate_directory, calibration_directory, *, snapshot=None, records=None):
        self.snapshot = snapshot or InputSnapshot()
        self.candidate_directory = Path(candidate_directory).resolve()
        self.directory = Path(calibration_directory).resolve()
        self.records = records or candidate_records(self.candidate_directory, self.snapshot)
        self.domains_path = ROOT / "benchmark/domains.json"
        catalog = self.snapshot.read(self.domains_path)
        self.domains = {entry["name"]: entry for entry in catalog["entries"]}
        self.manifest = self.snapshot.read(self.directory / "manifest.json")
        if not isinstance(self.manifest.get("configuration"), dict):
            raise ValueError("Calibration manifest is missing its configuration.")
        self.config = self.manifest["configuration"]
        self.configuration_sha256 = canonical_hash(self.config)
        if self.manifest.get("schema_version") != 1 or self.manifest.get("configuration_sha256") != self.configuration_sha256:
            raise ValueError("Calibration manifest configuration hash disagrees.")
        self.methods, self.seeds = self.config["methods"], self.config["seeds"]
        if (not isinstance(self.methods, list) or len(self.methods) != len(set(self.methods))
                or set(self.methods) - set(METHODS) or not self.methods):
            raise ValueError("Calibration manifest has invalid methods.")
        if (not isinstance(self.seeds, list) or not self.seeds or any(type(seed) is not int for seed in self.seeds)
                or len(self.seeds) != len(set(self.seeds))):
            raise ValueError("Calibration manifest seeds must be distinct integers.")
        if self.config.get("budget") != BUDGET or self.config.get("simulator_timeout_s") != 30:
            raise ValueError("Calibration manifest does not use the fixed evaluation contract.")
        names = self.config["candidate_ids"]
        if len(names) != len(set(names)) or set(names) - set(self.records):
            raise ValueError("Calibration candidate IDs disagree with the candidate index.")
        required = [self.candidate_directory / "index.json", self.domains_path,
                    *[ROOT / filename for filename in CODE_FILES],
                    *[self.records[name]["absolute_path"] for name in names]]
        declared_inputs = self.config["inputs_sha256"]
        for path in required:
            if str(path.resolve()) not in declared_inputs:
                raise ValueError("Calibration manifest omits a required input binding: " + str(path))
        for filename, expected in declared_inputs.items():
            self.snapshot.capture(filename, expected)
        self.physical_identities = self.manifest["physical_identities"]
        if self.config["physics"] != {key: canonical_hash(value) for key, value in self.physical_identities.items()}:
            raise ValueError("Calibration physical-identity hashes disagree.")
        for key, physical in self.physical_identities.items():
            if key != canonical_hash({name: physical[name] for name in PHYSICAL_FIELDS}):
                raise ValueError("Calibration physical identity is inconsistent with its circuit conditions.")
            if physical.get("cache_format_version") != 2 or physical["runtime"].get("simulator_timeout_s") != 30:
                raise ValueError("Unsupported physical simulation identity.")
            for filename, expected in {**physical["spice_dependency_sha256"], **physical["evaluator_sha256"]}.items():
                self.snapshot.capture(filename, expected)
            runtime = physical["runtime"]
            self.snapshot.capture(runtime["executable"], runtime["executable_sha256"])
            # Require actual dependency coverage, not merely an empty hash map.
            circuit = ROOT / physical["circuit_directory"]
            corner = ROOT / ".deps/sky130_pdk/libs.tech/ngspice/corners" / (physical["conditions"]["corner"] + ".spice")
            dependencies = {Path(path).resolve() for path in physical["spice_dependency_sha256"]}
            expected_dependencies = {path.resolve() for path in include_closure(
                [circuit / "netlist.spice", circuit / "reference.params", corner])}
            if dependencies != expected_dependencies:
                raise ValueError("Physical identity omits or changes circuit/model include dependencies.")
            expected_code = {str((ROOT / path).resolve()) for path in evaluator_hashes()}
            if not expected_code <= {str(Path(path).resolve()) for path in physical["evaluator_sha256"]}:
                raise ValueError("Physical identity omits evaluator bindings.")
        self.cache_directory = Path(self.config["cache_directory"]).resolve()
        self.measurements = {}
        self.summary = self.snapshot.read(self.directory / "summary.json")
        self.summary_rows = {}
        for row in self.summary["tasks"]:
            if row["id"] in self.summary_rows or row["id"] not in names:
                raise ValueError("Calibration summary contains duplicate or unconfigured task IDs.")
            self.summary_rows[row["id"]] = row
        if set(self.summary_rows) != set(names) or self.summary.get("methods") != self.methods or self.summary.get("seeds") != self.seeds or self.summary.get("budget") != BUDGET:
            raise ValueError("Calibration summary does not match its manifest.")

    def _checkpoint(self, path, identity):
        path = _inside(self.directory, str(path.relative_to(self.directory)))
        if not path.is_file():
            return None
        self.snapshot.read(path)  # Strict JSON and a before/after snapshot.
        return load_checkpoint(path, identity)

    def _measurement(self, record, physics, task, parameters):
        if record.get("parameters") != parameters:
            raise ValueError("Calibration observation parameter vector disagrees with its declared design.")
        key = canonical_hash({"identity": physics, "parameters": parameters})
        expected_directory = self.cache_directory / key[:2] / key
        if record.get("measurement_key") != key or (ROOT / record.get("measurement_directory", "")).resolve() != expected_directory:
            raise ValueError("Calibration physical measurement key or path disagrees.")
        if key not in self.measurements:
            cached = self.snapshot.read(expected_directory / "cache_entry.json")
            if cached.get("measurement_key") != key or cached.get("identity") != physics:
                raise ValueError("Physical cache entry has a different identity.")
            result_hash = cached.get("result_sha256")
            if not isinstance(result_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", result_hash):
                raise ValueError("Physical cache entry is missing a result hash.")
            self.snapshot.capture(expected_directory / "result.json", result_hash)
            self.measurements[key] = self.snapshot.read(expected_directory / "result.json")
        result = self.measurements[key]
        if result.get("parameters") != parameters:
            raise ValueError("Physical cache parameters disagree with the calibrated action.")
        if result.get("ngspice_version") != physics["runtime"]["ngspice_version"]:
            raise ValueError("Physical cache simulator version disagrees with calibration.")
        scored = rescore(result, task["constraints"])
        for field in ("status", "success", "reward", "metrics", "checks", "independent_metrics"):
            if record.get(field, {}) != scored.get(field, {}):
                raise ValueError("Calibration observation disagrees with its physical evidence: " + field)

    def task(self, name, *, require_complete=True):
        if name not in self.config["candidate_ids"]:
            raise ValueError("Selected candidate was not in this calibration.")
        indexed = self.records[name]
        candidate = self.snapshot.read(indexed["absolute_path"])
        task = candidate["task"]
        if candidate["id"] != name or task["id"] != name or candidate["topology"] != indexed["topology"]:
            raise ValueError("Candidate identity disagrees with its index.")
        topology = _identifier(candidate["topology"])
        family = _identifier(candidate["topology_family"])
        domain = self.domains[topology]
        _task_contract(task)
        self.snapshot.hashes.update(_source_identity(task, {"topology": topology, "topology_family": family}, domain))
        if set(candidate["reference"]) != set(task["parameters"]):
            raise ValueError("Candidate reference must contain the full parameter vector.")
        parameters_for(task, candidate["reference"])
        reference = rounded_parameters(task, candidate["reference"])
        if (rounded_parameters(task, task["initial_parameters"]) != task["initial_parameters"]
                or reference != candidate["reference"]):
            raise ValueError("Selected task/reference differs from its rounded calibrated form.")
        for values in (reference, domain["public_default"]):
            if set(values) != set(task["parameters"]):
                raise ValueError("Calibration requires complete parameter vectors.")
            parameters_for(task, values)
        physical_key = canonical_hash({field: task[field] for field in PHYSICAL_FIELDS})
        physics = self.physical_identities[physical_key]
        identity = {"configuration_sha256": self.configuration_sha256,
                    "candidate_sha256": digest(indexed["absolute_path"]), "task_sha256": canonical_hash(task),
                    "reference_sha256": canonical_hash(reference),
                    "public_default_sha256": canonical_hash(domain["public_default"]),
                    "physics_sha256": canonical_hash(physics)}
        admission_path = self.directory / name / "admission.json"
        admission = self._checkpoint(admission_path, {**identity, "kind": "admission"})
        if admission is None:
            if not require_complete:
                return None
            raise ValueError("Missing or incomplete admission checkpoint: " + name)
        if type(admission.get("admitted")) is not bool:
            raise ValueError("Admission decision must be Boolean.")
        if (admission.get("id") != name or admission.get("topology") != topology or
                admission.get("effective_task") != task or admission.get("rounded_reference_parameters") != reference or
                admission.get("public_default_parameters") != domain["public_default"] or admission.get("logical_evaluations") != 3):
            raise ValueError("Admission task, source or parameter vectors disagree.")
        for phase, parameters in (("initial", task["initial_parameters"]), ("reference", reference), ("public_seed", domain["public_default"])):
            measured = admission["evaluations"][phase]
            validate_evaluation(measured, task)
            self._measurement(measured, physics, task, parameters)
        decision = admission_decision(task, **{name: admission["evaluations"][name]
                                               for name in ("initial", "reference", "public_seed")})
        if any(admission.get(key) != value for key, value in decision.items()):
            raise ValueError("Admission claims disagree with independently recomputed decisions.")
        summary = self.summary_rows[name]
        if summary.get("admitted") is not decision["admitted"]:
            raise ValueError("Summary admission disagrees with its checkpoint.")
        if (any(summary.get(field) != candidate.get(field) for field in
                ("topology", "topology_family", "proposed_difficulty", "profile"))
                or summary.get("rejection_reasons") != decision["rejection_reasons"]
                or summary.get("expected_trials") != (len(self.methods) * len(self.seeds) if decision["admitted"] else 0)):
            raise ValueError("Calibration summary task metadata or expected trial count disagrees.")
        trials, files, methods = [], [admission_path], {}
        for method in self.methods:
            method_trials = []
            for seed in self.seeds:
                path = self.directory / name / f"{method}_{seed}.json"
                trial = self._checkpoint(path, {**identity, "kind": "baseline", "method": method, "seed": seed})
                if trial is None:
                    if require_complete and decision["admitted"]:
                        raise ValueError("Missing or incomplete baseline trial: " + str(path))
                    continue
                validate_trial(trial, task, method, seed, lambda observation, values: self._measurement(observation, physics, task, values))
                method_trials.append(trial)
                files.append(path)
            methods[method] = summarize_trials(method_trials)
            trials.extend(method_trials)
        if summary.get("methods") != methods or summary.get("pooled") != summarize_trials(trials):
            raise ValueError("Calibration summary statistics disagree with verified trial checkpoints.")
        return {"candidate": candidate, "admission": admission, "observation": {**summary, "methods": methods},
                "calibration_directory": str(self.directory), "checkpoint_files": files,
                "level": observed_level(methods), "configuration_sha256": self.configuration_sha256}


def assemble(candidate_directory, selection_path, output):
    candidate_directory, selection_path, output = [Path(value).resolve() for value in
                                                   (candidate_directory, selection_path, output)]
    if output.exists():
        raise FileExistsError("Release output must be new.")
    snapshot = InputSnapshot()
    selection = snapshot.read(selection_path)
    if selection.get("status") != "selection_complete" or not isinstance(selection.get("tasks"), list) or len(selection["tasks"]) != 250:
        raise ValueError("The release requires a complete 250-task selection.")
    selected_ids = [_identifier(row["id"]) for row in selection["tasks"]]
    if len(set(selected_ids)) != 250:
        raise ValueError("The release requires 250 distinct task IDs.")
    if Counter(row["difficulty"] for row in selection["tasks"]) != REQUIRED_COUNTS:
        raise ValueError("The release requires exactly 100 easy, 125 medium, and 25 hard tasks.")
    candidates = candidate_records(candidate_directory, snapshot)
    if set(selected_ids) - set(candidates):
        raise ValueError("Selected task IDs are missing from the candidate index.")
    domains = {row["name"]: row for row in snapshot.read(ROOT / "benchmark/domains.json")["entries"]}
    prepared = []
    contexts, seen_references, seen_groups, vectors = {}, set(), set(), {}
    for selected in selection["tasks"]:
        calibration = Path(selected["calibration_directory"]).resolve()
        if calibration not in contexts:
            contexts[calibration] = CalibrationEvidence(candidate_directory, calibration, snapshot=snapshot, records=candidates)
        audited = contexts[calibration].task(selected["id"])
        candidate, row, admission = audited["candidate"], audited["observation"], audited["admission"]
        level = observed_level(row["methods"], minimum_trials=10, hard_trials=20)
        if not row["admitted"] or level != selected["difficulty"] or level not in {"easy", "medium", "hard"}:
            raise ValueError("Missing confirmed difficulty or admission: " + candidate["id"])
        if not admission["admitted"] or admission["public_seed_status"] != "valid_failure":
            raise ValueError("A release task needs a valid, unsuccessful public-seed replay.")
        _, failure_reason = admission_failures(candidate, admission)
        curation = start_curation(candidate, level)
        if failure_reason or not curation["start_change_curation_passed"]:
            raise ValueError("Selected task fails the conservative start-curation checks: " + candidate["id"])
        if selected.get("topology", candidate["topology"]) != candidate["topology"] or selected.get("observed_level", level) != level:
            raise ValueError("Selection labels disagree with audited calibration.")
        ref_key = (candidate["topology"], canonical_hash(candidate["reference"]))
        group = canonical_hash(requirement_identity(candidate["task"]))
        if ref_key in seen_references or group in seen_groups:
            raise ValueError("Release repeats a reference vector or requirement group.")
        from benchmark.candidates import distance
        if any(distance(candidate["task"], candidate["reference"], previous) < 0.025
               for previous in vectors.get(candidate["topology"], [])):
            raise ValueError("Release references violate the minimum normalized distance.")
        vectors.setdefault(candidate["topology"], []).append(candidate["reference"])
        seen_references.add(ref_key)
        seen_groups.add(group)
        # Snapshot exact validated checkpoint bytes; never copy arbitrary JSONs.
        checkpoint_bytes = {path.name: path.read_bytes() for path in audited["checkpoint_files"]}
        prepared.append((candidate, selected, calibration, row, admission, checkpoint_bytes, curation))
    snapshot.assert_unchanged()
    output.mkdir(parents=True, exist_ok=False)
    index = {"schema_version": 1, "collection": output.name,
             "created_utc": datetime.now(timezone.utc).isoformat(),
             "status": "awaiting_independent_release_verification", "task_count": 250,
             "scope": "Nominal transistor-level amplifier sizing using fixed SKY130 models and testbenches.",
             "generation_mode": "source_grounded_simulation_guided_deterministic_curation",
             "difficulty_scope": "Relative to the declared search baselines at 30 evaluations; untrained-LLM difficulty is not measured.",
             "split_policy": "topology_family",
             "training_approved": False, "model_training_performed": False, "tasks": []}
    calibration_manifests = {}
    for candidate, selected, calibration, observation, admission, checkpoint_bytes, curation in prepared:
        task_id = candidate["id"]
        task_path = "tasks/" + task_id + ".json"
        reference_path = "private/" + task_id + "/reference.json"
        save_json(output / task_path, candidate["task"])
        save_json(output / reference_path, candidate["reference"])
        provenance = {"generation_mode": candidate["generation_mode"],
                      "proposed_difficulty": candidate["proposed_difficulty"],
                      "measured_difficulty": selected["difficulty"], "profile": candidate["profile"],
                      "candidate_sha256": digest(candidate_directory / candidates[task_id]["path"]),
                      "characterization_reference": candidate["characterization_reference"],
                      "characterization_start": candidate["characterization_start"],
                      "empirical_solution_signature": candidate["empirical_solution_signature"],
                      "numeric_target_origin": "Targets derived from actual simulation measurements and rounded to declared engineering grids; not recovered paper performance.",
                      "default_reference_reused": False, "training_approved": False,
                      "start_curation": curation, "start_perturbation": candidate.get("start_perturbation"),
                      "admission": admission, "baseline_summary": observation}
        save_json(output / "private" / task_id / "provenance.json", provenance)
        evidence = output / "private" / task_id / "baselines"
        evidence.mkdir()
        copied_trials = []
        for name, content in sorted(checkpoint_bytes.items()):
            if name == "admission.json":
                (output / "private" / task_id / "admission_checkpoint.json").write_bytes(content)
                continue
            destination = evidence / name
            destination.write_bytes(content)
            copied_trials.append({"path": str(destination.relative_to(output)), "sha256": digest(destination)})
        split = SPLITS.get(candidate["topology_family"], "train")
        entry = {"id": task_id, "path": task_path, "sha256": digest(output / task_path),
                 "reference_path": reference_path, "reference_sha256": digest(output / reference_path),
                 "topology": candidate["topology"], "topology_family": candidate["topology_family"],
                 "difficulty": selected["difficulty"], "split": split,
                 "requirement_group": canonical_hash(requirement_identity(candidate["task"])),
                 "profile": candidate["profile"], "parameter_count": len(candidate["task"]["parameters"]),
                 "start_variant": curation["start_variant"],
                 "substantive_changed_control_count": curation["substantive_changed_control_count"],
                 "start_change_curation_passed": curation["start_change_curation_passed"],
                 "initial_failed_requirements": [name for name, passed in admission["evaluations"]["initial"]["checks"].items() if not passed],
                 "baseline_summary": observation["methods"], "baseline_files": copied_trials,
                 "source_record": "sources/" + candidate["topology"] + ".json"}
        index["tasks"].append(entry)
        source = domains[candidate["topology"]]["source"]
        save_json(output / entry["source_record"], source)
        calibration_manifests[str(calibration)] = digest(calibration / "manifest.json")
    index["difficulty_counts"] = dict(Counter(row["difficulty"] for row in index["tasks"]))
    index["split_counts"] = dict(Counter(row["split"] for row in index["tasks"]))
    index["topology_counts"] = dict(Counter(row["topology"] for row in index["tasks"]))
    index["topology_count"] = len(index["topology_counts"])
    index["requirement_group_count"] = len({row["requirement_group"] for row in index["tasks"]})
    index["distinct_reference_count"] = len({(row["topology"], row["reference_sha256"]) for row in index["tasks"]})
    snapshot.assert_unchanged()
    save_json(output / "splits.json", {"strategy": "whole_topology_family_holdout", "assignment": SPLITS,
                                       "default_split": "train", "tasks": {row["id"]: row["split"] for row in index["tasks"]}})
    save_json(output / "build_manifest.json", {"selection_sha256": digest(selection_path),
                                               "candidate_index_sha256": digest(candidate_directory / "index.json"),
                                               "domains_sha256": digest(ROOT / "benchmark/domains.json"),
                                               "evaluator_sha256": evaluator_hashes(),
                                               "benchmark_code_sha256": {str(path.relative_to(ROOT)): digest(path) for path in sorted((ROOT / "benchmark").glob("*.py"))},
                                               "calibration_manifests_sha256": calibration_manifests,
                                               "verified_calibration_inputs_sha256": snapshot.hashes,
                                               "training_qualification_sha256": digest(ROOT / "verification/qualification.json")})
    save_json(output / "index.json", index)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    index = assemble(args.candidates, args.selection, args.output)
    print(json.dumps({key: index[key] for key in ("status", "task_count", "difficulty_counts", "split_counts", "topology_counts")}, indent=2))


if __name__ == "__main__":
    main()
