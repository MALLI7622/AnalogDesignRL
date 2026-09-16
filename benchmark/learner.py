"""Export answer-free benchmark specifications and expose context in episodes.

Whitelisting limits the exported artifact. It does not sandbox a learner process
or prevent that process from reading other files to which it already has access.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from analog_design.episode import Episode
from analog_design.model_clients import strict_json
from analog_design.simulator import parameters_for


_CONDITIONS = ("corner", "supply_v", "temperature_c", "common_mode_v", "load_f")
_AC = ("points_per_decade", "start_hz", "stop_hz")
_TRANSIENT = ("low_v", "high_v", "rise_start_s", "edge_s", "high_duration_s", "period_s",
              "stop_s", "max_step_s", "settling_tolerance_v", "minimum_hold_s")
_PARAMETER_RULE = ("min", "max", "unit", "integer", "role", "sampling_scale")
_CONTEXT = ("topology", "topology_family", "circuit_description", "paper", "netlist", "fixed_parameters", "scope")
_PAPER = ("title", "authors", "year", "doi", "source_url", "figure")
_PRIVATE_NAMES = {"reference", "references", "reference_parameters", "reference_metrics", "reference_path",
                  "reference_sha256", "solution", "solutions", "solution_parameters", "solution_metrics",
                  "solution_path", "default", "defaults", "default_parameters", "public_default",
                  "seed_reference_parameters", "seed_parameters", "trajectory", "trajectories",
                  "baseline_trajectories", "private", "private_parameters", "provenance"}


def _reject_private_fields(value, location="task"):
    if isinstance(value, dict):
        for name, child in value.items():
            if not isinstance(name, str):
                raise ValueError("Learner JSON object keys must be strings.")
            normalized = name.lower().replace("-", "_")
            if normalized in _PRIVATE_NAMES or normalized.startswith(("reference_", "solution_", "default_")):
                raise ValueError(f"Answer-bearing field is not permitted in learner input: {location}.{name}")
            _reject_private_fields(child, location + "." + name)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_private_fields(child, location + f"[{index}]")


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Expected finite numeric value: {name}")


def _public_context(context, editable):
    if not isinstance(context, dict):
        raise ValueError("design_context must be an object.")
    _reject_private_fields(context, "design_context")
    safe = {name: deepcopy(context[name]) for name in _CONTEXT if name in context}
    # Accept the documented input alias; publish one consistent field name.
    if "topology_family" not in safe and "family" in context:
        safe["topology_family"] = deepcopy(context["family"])
    for name in ("topology", "topology_family", "circuit_description", "scope"):
        if name in safe and (not isinstance(safe[name], str) or not safe[name].strip()):
            raise ValueError(f"Public context {name} must be nonempty text.")
    paper = safe.get("paper", {})
    if not isinstance(paper, dict):
        raise ValueError("Public paper attribution must be an object.")
    safe["paper"] = {name: deepcopy(paper[name]) for name in _PAPER if name in paper}
    for name, value in safe["paper"].items():
        if name == "authors":
            if not isinstance(value, list) or not all(isinstance(author, str) and author.strip() for author in value):
                raise ValueError("Paper authors must be a list of names.")
        elif name == "year":
            if type(value) is not int or value < 1:
                raise ValueError("Paper year must be a positive integer.")
        elif name == "figure":
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError("Paper figure must be a string or integer.")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"Paper {name} must be nonempty text.")
        elif name == "source_url":
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Paper source_url must be a public HTTP(S) attribution URL.")
    fixed = safe.get("fixed_parameters", {})
    if not isinstance(fixed, dict) or {name.lower() for name in fixed} & {name.lower() for name in editable}:
        raise ValueError("fixed_parameters must not contain any editable parameter value.")
    for name, value in fixed.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid fixed parameter name.")
        _finite(value, "fixed_parameters." + name)
    safe["fixed_parameters"] = fixed
    netlist = safe.get("netlist")
    if not isinstance(netlist, str) or not netlist.strip():
        raise ValueError("Public design context requires a symbolic circuit netlist.")
    if re.search(r"(?im)^\s*\.(?:include|inc|lib|control|endc)\b", netlist):
        raise ValueError("Public circuit netlists may not include external files or control programs.")
    for name in editable:
        variable = rf"(?<!\w){re.escape(name)}(?!\w)"
        if not re.search(variable, netlist, re.IGNORECASE):
            raise ValueError(f"Editable parameter is missing from the symbolic netlist: {name}")
        if re.search(variable + r"\s*=", netlist, re.IGNORECASE):
            raise ValueError(f"Public netlist assigns a value to an editable parameter: {name}")
    return safe


def public_specification(task):
    """Return a defensive, explicitly whitelisted initial learner observation.

    Trusted simulation task paths and source records are omitted. Structured
    answer/default/trajectory fields are rejected, including in nested metadata.
    The initial design is public by design; passing witnesses are never read.
    """
    if not isinstance(task, dict):
        raise ValueError("A task must be an object.")
    _reject_private_fields(task)
    if not isinstance(task.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task["id"]):
        raise ValueError("Invalid public task ID.")
    if not isinstance(task.get("purpose"), str) or not task["purpose"].strip():
        raise ValueError("Public task purpose must be nonempty text.")
    budget = task["max_evaluations"]
    if type(budget) is not int or budget < 1:
        raise ValueError("max_evaluations must be a positive integer.")
    if not isinstance(task["initial_parameters"], dict) or set(task["initial_parameters"]) != set(task["parameters"]):
        raise ValueError("Initial values must cover exactly the editable parameters.")
    parameters = parameters_for(task, {})
    rules = {}
    for name, rule in task["parameters"].items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid editable parameter name.")
        for field in ("min", "max"):
            _finite(rule[field], name + "." + field)
        if not isinstance(rule.get("unit"), str) or not rule["unit"].strip():
            raise ValueError("Editable parameters need explicit units.")
        if "integer" in rule and type(rule["integer"]) is not bool:
            raise ValueError("The integer parameter rule must be Boolean.")
        for field in ("role", "sampling_scale"):
            if field in rule and not isinstance(rule[field], str):
                raise ValueError(f"Parameter {field} must be text.")
        rules[name] = {field: deepcopy(rule[field]) for field in _PARAMETER_RULE if field in rule}
    safe = {"id": task["id"], "purpose": task["purpose"],
            "conditions": {field: deepcopy(task["conditions"][field]) for field in _CONDITIONS},
            "parameters": rules, "constraints": {},
            "ac": {field: deepcopy(task["ac"][field]) for field in _AC},
            "transient": {field: deepcopy(task["transient"][field]) for field in _TRANSIENT},
            "max_evaluations": budget, "current_parameters": parameters,
            "evaluations_remaining": budget}
    for metric, limits in task["constraints"].items():
        if not isinstance(limits, dict) or not limits or set(limits) - {"min", "max"}:
            raise ValueError("Metric constraints must contain only min/max numeric targets.")
        for target in limits.values():
            _finite(target, metric)
            if target <= 0:
                raise ValueError("Metric targets must be positive.")
        safe["constraints"][metric] = deepcopy(limits)
    if not safe["constraints"]:
        raise ValueError("A learner task must specify performance requirements.")
    if "design_context" in task:
        safe["design_context"] = _public_context(task["design_context"], rules)
    _reject_private_fields(safe, "public_specification")
    return json.loads(json.dumps(safe, allow_nan=False))


_README = """# Analog benchmark learner inputs

This package contains initial specifications, circuit context, and evaluation
rules. The JSON files are learner observations, not executable simulator tasks.
Use the trusted evaluator to apply actions and measure circuits.

Reply with a JSON object mapping allowed parameter names to numeric absolute
values. Use the units in each parameter rule. Omitted values retain their current
values; `{}` evaluates the current design. Integer multiplicities must be whole
numbers. Requirements, circuit connections, test conditions, and the evaluation
budget are fixed. Every submitted action consumes one evaluation, including an
invalid action or unsuccessful simulation. The episode ends on success or when
its budget is exhausted.

All constraints must pass. Successful evaluations receive reward 1. Otherwise the
reward is the negative mean of normalized violations, each capped at 1. Invalid
parameters, invalid measurements, and failed simulations receive -1. Compare
methods using the same action budget and the same initial-evaluation convention.

The benchmark baselines spend evaluation 1 on the supplied starting design. For
the same convention, the trusted episode driver must call `episode.step({})`
before the learner proposes changes and supply that result with the current
specification. This leaves 29 evaluations. Constructing an Episode or
BenchmarkEpisode does not perform this initial evaluation automatically.

| Metric | Meaning under the stated test conditions |
|---|---|
| gain_db | Open-loop gain at the AC sweep's starting frequency, in dB. |
| unity_gain_hz | The measured single downward unity-gain crossing, in Hz. |
| phase_margin_deg | Phase margin at that crossing, in degrees. |
| power_w | Quiescent DC supply power, in watts. |
| dc_error_v | Absolute output/common-mode error at the DC operating point. |
| max_tracking_error_v | Maximum tracking error in the specified steady portions of the input waveform. |
| settling_rise_s / settling_fall_s | Time after the corresponding edge to enter and remain in the explicit absolute settling band for the required hold time. |

The transient settling band is `settling_tolerance_v`; it is distinct from the
maximum tracking-error requirement. Circuit provenance and scope describe which
aspects are source-based and which are project-defined. A task's difficulty is
relative to the calibration methods reported with the benchmark, and does not
establish universal optimizer hardness or manufactured-circuit performance.

For integration inside the trusted evaluator process:

```python
from benchmark.learner import BenchmarkEpisode

episode = BenchmarkEpisode(task_path, output_directory, for_training=True)
observation = episode.step({})
specification = episode.specification()
```

`task_path` above is the trusted executable task retained by the evaluator. Send
only the returned specifications, numeric actions, and observations across the
learner interface. Training mode retains the existing explicit qualification
gate; package creation does not grant approval. Evaluation mode is available for
diagnostics by omitting `for_training=True`.

This export limits package contents. It does not provide operating-system
isolation for a process that can access other files on the same machine.
"""


def export_learner_bundle(index_path, output_directory):
    """Verify task hashes and export only specifications, a small index, and README.

    Index entries may contain evaluator-only fields, but those files are never
    opened or copied. All inputs are validated before creating the new output.
    """
    index_path = Path(index_path).resolve()
    output = Path(output_directory).resolve()
    if output.exists():
        raise FileExistsError(f"Learner output directory already exists: {output}")
    index = strict_json(index_path.read_text())
    if not isinstance(index.get("tasks"), list) or not index["tasks"]:
        raise ValueError("The release index needs a nonempty task list.")
    records, ids = [], set()
    for entry in index["tasks"]:
        name = entry["id"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in ids:
            raise ValueError("Duplicate or invalid task ID in release index.")
        ids.add(name)
        if entry["path"] != f"tasks/{name}.json":
            raise ValueError("Unexpected executable task path in release index.")
        source = (index_path.parent / entry["path"]).resolve()
        if not source.is_relative_to(index_path.parent):
            raise ValueError("Executable task path escapes the release directory.")
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("Task content changed since the release index was written.")
        task = strict_json(content.decode())
        if task["id"] != name:
            raise ValueError("Executable task ID disagrees with the release index.")
        spec = public_specification(task)
        family = entry.get("topology_family", entry.get("family"))
        for value in (entry["topology"], family):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Topology and family must be nonempty text.")
        if entry["difficulty"] not in {"easy", "medium", "hard"} or entry["split"] not in {"train", "validation", "test"}:
            raise ValueError("Invalid difficulty or split in the release index.")
        context = spec.get("design_context")
        if context is not None and (context.get("topology") != entry["topology"] or context.get("topology_family") != family):
            raise ValueError("Circuit context disagrees with release topology metadata.")
        serialized = (json.dumps(spec, indent=2, allow_nan=False) + "\n").encode()
        records.append(({"id": name, "path": f"tasks/{name}.json", "sha256": hashlib.sha256(serialized).hexdigest(),
                         "topology": entry["topology"], "family": family,
                         "difficulty": entry["difficulty"], "split": entry["split"]}, serialized))
    output.mkdir(parents=True, exist_ok=False)
    (output / "tasks").mkdir()
    for entry, content in records:
        (output / entry["path"]).write_bytes(content)
    public_index = {"schema_version": 1, "artifact": "learner_specifications", "task_count": len(records),
                    "tasks": [entry for entry, _ in records]}
    (output / "index.json").write_text(json.dumps(public_index, indent=2) + "\n")
    (output / "README.md").write_text(_README)
    return public_index


class BenchmarkEpisode(Episode):
    """An Episode with sanitized circuit context in its learner specification.

    The constructor is inherited unchanged, including its training-approval gate.
    This class does not isolate the trusted evaluator's filesystem or processes.
    """

    def specification(self):
        current = super().specification()
        public = public_specification(self.task)
        public["current_parameters"] = current["current_parameters"]
        public["evaluations_remaining"] = current["evaluations_remaining"]
        return public
