"""Write private human-readable feasible-witness notes after fresh verification.

Validate every task and render every note before writing any solution file.
This consumes verifier evidence; it does not run simulations, certify optimality,
assign difficulty, approve training, or modify a learner package.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

from analog_design.model_clients import strict_json
from benchmark.candidates import reference_margin
from benchmark.verify_release import METRIC_DIRECTIONS, _check_measurement, _hash, _parameters


METRIC_UNITS = {"gain_db": "dB", "unity_gain_hz": "Hz", "phase_margin_deg": "deg", "power_w": "W",
                "dc_error_v": "V", "max_tracking_error_v": "V", "settling_rise_s": "s", "settling_fall_s": "s"}
PHASES = (("initial", False), ("reference", True), ("refined_reference", True))


class _Snapshot:
    def __init__(self):
        self.hashes = {}

    def read(self, path, expected=None):
        path = Path(path).resolve()
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if (expected is not None and expected != actual) or self.hashes.get(str(path), actual) != actual:
            raise ValueError("Artifact hash changed or disagrees with its binding: " + str(path))
        self.hashes[str(path)] = actual
        return strict_json(content.decode())

    def assert_unchanged(self):
        for filename, expected in self.hashes.items():
            if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
                raise ValueError("Solution-note input changed: " + filename)


def _inside(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Artifact paths must be relative strings.")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path escapes its directory.")
    return path


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers, rows):
    return "\n".join(["| " + " | ".join(map(_cell, headers)) + " |",
                      "| " + " | ".join("---" for _ in headers) + " |"] +
                     ["| " + " | ".join(map(_cell, row)) + " |" for row in rows])


def _number(value):
    return format(value, ".15g")


def _link(label, path, directory):
    relative = os.path.relpath(path, directory)
    return "[" + _cell(label) + "](" + quote(relative, safe="/") + ")"


def _slack(metrics, constraints, name):
    direction, target = next(iter(constraints[name].items()))
    return metrics[name] - target if direction == "min" else target - metrics[name]


def prepare_notes(bundle, verification, *, expected_count=250):
    """Return validated note inputs plus their immutable input snapshot."""
    if type(expected_count) is not int or expected_count < 1:
        raise ValueError("expected_count must be a positive integer.")
    bundle, verification = Path(bundle).resolve(), Path(verification).resolve()
    snapshot = _Snapshot()
    index = snapshot.read(bundle / "index.json")
    audit = snapshot.read(verification)
    entries, results = index.get("tasks"), audit.get("tasks")
    if (not isinstance(entries, list) or len(entries) != expected_count or index.get("task_count") != expected_count
            or not isinstance(results, list) or len(results) != expected_count or audit.get("task_count") != expected_count):
        raise ValueError("The complete expected bundle and verification task counts must agree.")
    if (audit.get("status") != "verified" or audit.get("publication_ready") is not True
            or audit.get("scope") != "fresh_simulation_and_static" or audit.get("simulations_requested") is not True
            or audit.get("errors")):
        raise ValueError("Solution notes require a successful complete fresh verification report.")
    ids = [entry["id"] for entry in entries]
    verified_ids = [row["id"] for row in results]
    if (len(set(ids)) != expected_count or len(set(verified_ids)) != expected_count or set(ids) != set(verified_ids)
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in ids)):
        raise ValueError("Bundle and verification task identities must agree exactly and be distinct.")
    captured = audit.get("inputs_sha256", {})
    original_index = audit.get("index_path")
    if not original_index or captured.get(original_index) != snapshot.hashes[str(bundle / "index.json")]:
        raise ValueError("Verification is not bound to this exact bundle index.")
    original_directory = Path(original_index).parent
    verified = {row["id"]: row for row in results}
    private = bundle / "private"
    if private.is_symlink():
        raise ValueError("The private notes directory must not be a symlink.")
    prepared = []
    for entry in entries:
        task_id = entry["id"]
        if entry["path"] != f"tasks/{task_id}.json" or entry["reference_path"] != f"private/{task_id}/reference.json":
            raise ValueError("Task/reference paths disagree with the private release layout.")
        task_path, reference_path = (_inside(bundle, entry[key]) for key in ("path", "reference_path"))
        for field, hash_field in (("path", "sha256"), ("reference_path", "reference_sha256")):
            if captured.get(str(original_directory / entry[field])) != entry[hash_field]:
                raise ValueError("Fresh verification omits a task/reference hash binding: " + task_id)
        task, reference = snapshot.read(task_path, entry["sha256"]), snapshot.read(reference_path, entry["reference_sha256"])
        if task.get("id") != task_id or set(task.get("constraints", {})) != set(METRIC_DIRECTIONS):
            raise ValueError("Task identity or eight-metric constraint contract disagrees.")
        for name, direction in METRIC_DIRECTIONS.items():
            if set(task["constraints"][name]) != {direction}:
                raise ValueError("Unexpected metric requirement direction: " + name)
        _parameters(task, task["initial_parameters"], "initial")
        _parameters(task, reference, "reference")
        if _hash(task["initial_parameters"]) == _hash(reference):
            raise ValueError("The passing witness cannot equal its failed initial design.")
        result = verified[task_id]
        if result.get("status") != "verified" or result.get("errors") or set(result.get("evaluations", {})) != {name for name, _ in PHASES}:
            raise ValueError("Every task needs complete successful initial/reference/refined verification.")
        run = _inside(verification.parent, "tasks/" + task_id)
        task_report_path = run / "verification.json"
        if snapshot.read(task_report_path) != result:
            raise ValueError("Per-task verification record differs from the complete report: " + task_id)
        copied_task_path, refined_path = run / "task.json", run / "refined_task.json"
        refined = deepcopy(task)
        refined["ac"]["points_per_decade"] *= 2
        refined["transient"]["max_step_s"] /= 2
        if snapshot.read(copied_task_path) != task or snapshot.read(refined_path) != refined:
            raise ValueError("Fresh-run task definitions differ from the indexed task or declared refinement.")
        checks, evidence = {}, [("Bundle index", bundle / "index.json"), ("Task", task_path),
                                ("Reference parameters", reference_path), ("Complete fresh verification", verification),
                                ("Task verification", task_report_path), ("Nominal run task", copied_task_path),
                                ("Refined run task", refined_path)]
        for phase, expected in PHASES:
            path = run / (phase + "_returned_result.json")
            if not (run / phase).is_dir():
                raise ValueError("A fresh simulation run directory is missing: " + task_id + "/" + phase)
            raw = snapshot.read(path)
            definition = refined if phase == "refined_reference" else task
            run_task_path = refined_path if phase == "refined_reference" else copied_task_path
            values = task["initial_parameters"] if phase == "initial" else reference
            if (raw.get("task_id") != task_id or raw.get("task_sha256") != snapshot.hashes[str(run_task_path)]
                    or _hash(raw.get("parameters")) != _hash(values)):
                raise ValueError("Fresh result is not bound to the expected task and parameter vector: " + task_id + "/" + phase)
            checked = _check_measurement(raw, definition, expected)
            if checked != result["evaluations"][phase]:
                raise ValueError("Rescored fresh result differs from reported measurements: " + task_id + "/" + phase)
            checks[phase] = checked
            evidence.append((phase + " returned result", path))
        note_directory = private / task_id
        note_path = note_directory / "SOLUTION.md"
        if note_directory.is_symlink() or note_path.is_symlink() or not note_directory.resolve().is_relative_to(private.resolve()):
            raise ValueError("Solution content must remain in its private task directory.")
        identity = audit.get("runtime_identities", {}).get(entry["topology"])
        if not isinstance(identity, dict) or not identity:
            raise ValueError("Fresh verification is missing this topology's runtime identity.")
        prepared.append({"id": task_id, "entry": entry, "task": task, "reference": reference,
                         "evaluations": checks, "run": run, "note_path": note_path,
                         "evidence": [(label, path, snapshot.hashes[str(path)]) for label, path in evidence],
                         "verification_finished_utc": audit.get("finished_utc", "not recorded"),
                         "runtime_identity_sha256": _hash(identity)})
    snapshot.assert_unchanged()
    return prepared, snapshot


def render_solution(record):
    task, reference, evaluations = record["task"], record["reference"], record["evaluations"]
    constraints, directory = task["constraints"], record["note_path"].parent
    initial, nominal, refined = (evaluations[name] for name in ("initial", "reference", "refined_reference"))
    failed = [name for name, passed in initial["checks"].items() if not passed]
    parameters = [(name, rule["unit"], rule.get("role", "Role not recorded"), _number(task["initial_parameters"][name]),
                   _number(reference[name])) for name, rule in task["parameters"].items()]
    rows = []
    for name, direction in METRIC_DIRECTIONS.items():
        target = constraints[name][direction]
        slack = [min(_slack(result[field], constraints, name) for field in ("metrics", "independent_metrics"))
                 for result in (nominal, refined)]
        rows.append((name, METRIC_UNITS[name], ("≥ " if direction == "min" else "≤ ") + _number(target),
                     *(_number(result["metrics"][name]) for result in (initial, nominal, refined)),
                     *(_number(value) for value in slack)))
    margin = {phase: all(reference_margin(evaluations[phase][field], constraints)
                         for field in ("metrics", "independent_metrics")) for phase in ("reference", "refined_reference")}
    gain_change = nominal["metrics"]["gain_db"] - initial["metrics"]["gain_db"]
    power_change = nominal["metrics"]["power_w"] - initial["metrics"]["power_w"]
    return "\n".join(["# Private solution evidence: " + record["id"], "",
        "This is **one verified feasible parameter vector**, not an optimum or a unique solution. "
        "It applies to the project's nominal circuit adaptation and fixed testbench; these numbers do not reproduce "
        "the original paper's performance. Keep this note and its linked evidence outside the learner's accessible files.", "",
        "## Measured result", "",
        "The initial design fails exactly: " + ", ".join("`" + name + "`" for name in failed) + ". "
        f"The reference passes all {len(constraints)} requirements at both nominal and refined resolution, "
        "with matching per-constraint decisions from waveform extraction and independent native measurements.", "",
        f"From the initial design to the nominal reference, measured gain changes by {gain_change:+.8g} dB and "
        f"quiescent power by {power_change:+.8g} W. The table records the remaining measured changes. "
        "These two design evaluations do not isolate the causal effect of any individual parameter.", "",
        "## Parameter values", "",
        _table(["Control", "Unit", "Recorded role", "Initial", "Feasible reference"], parameters), "",
        "Exact machine-readable values: " + _link("reference.json", directory / "reference.json", directory) + ".", "",
        "## Requirements, measurements, and slack", "",
        _table(["Metric", "Unit", "Requirement", "Initial", "Reference", "Refined reference",
                "Reference worst-path slack", "Refined worst-path slack"], rows), "",
        "Displayed measurements use the waveform extractor. Slack is the smaller signed margin over the waveform "
        "and native extraction paths: measured minus minimum, or maximum minus measured. Nonnegative slack meets "
        "the requirement. Numerical construction-margin checks recomputed on both paths: nominal reference **" +
        ("met" if margin["reference"] else "not met") + "**; refined reference **" +
        ("met" if margin["refined_reference"] else "not met") + "**. These margin flags are reported separately "
        "from the passing feasibility decisions; they do not create an additional acceptance rule.", "",
        "Refinement doubles AC points per decade and halves the maximum transient step; the same targets and "
        "reference parameters pass at both resolutions. This is a finite-resolution check, not a proof of convergence. "
        "`power_w` is DC quiescent supply power. Gain and settling follow this task's declared AC frequency and "
        "absolute settling band; they are not interchangeable with other testbench definitions.", "",
        "## Exact evidence and run bindings", "",
        "Fresh verification finished: `" + str(record["verification_finished_utc"]) + "`. "
        "Runtime identity fingerprint (normalized JSON SHA-256): `" + record["runtime_identity_sha256"] + "` "
        "(the complete report records simulator/model/evaluator identity for this topology).", "",
        _table(["Artifact", "SHA-256"], [(_link(label, path, directory), "`" + digest + "`")
                                         for label, path, digest in record["evidence"]]), "",
        "Fresh simulation directories: " + ", ".join(_link(phase, record["run"] / phase, directory) for phase, _ in PHASES) + ".", ""])


def write_solution_notes(bundle, verification, *, expected_count=250):
    prepared, snapshot = prepare_notes(bundle, verification, expected_count=expected_count)
    rendered = [(record["note_path"], render_solution(record)) for record in prepared]
    # Reject stale inputs or conflicting hand-written notes before writing any task.
    for path, content in rendered:
        if path.exists() and path.read_text() != content:
            raise ValueError("An existing private solution note differs from this evidence: " + str(path))
    destination = Path(bundle).resolve() / "private/solution_notes_manifest.json"
    if destination.is_symlink():
        raise ValueError("The private solution manifest must not be a symlink.")
    snapshot.assert_unchanged()
    manifest = {"schema_version": 1, "status": "complete", "task_count": len(rendered),
                "scope": "Private human-readable feasible witnesses, not optimality or training approval.",
                "inputs_sha256": snapshot.hashes,
                "notes": [{"id": record["id"], "path": str(path.relative_to(Path(bundle).resolve())),
                           "sha256": hashlib.sha256(content.encode()).hexdigest()}
                          for record, (path, content) in zip(prepared, rendered)]}
    for path, content in rendered:
        if not path.exists():
            path.write_text(content)
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=250)
    args = parser.parse_args()
    result = write_solution_notes(args.bundle, args.verification, expected_count=args.expected_count)
    print(json.dumps({"status": result["status"], "task_count": result["task_count"], "output": "private/<task_id>/SOLUTION.md"}))


if __name__ == "__main__":
    main()
