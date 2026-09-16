"""Content-addressed, losslessly archived simulator measurements for benchmark work.

Measurements may be rescored against different targets only when circuit, bounds,
conditions, and measurement settings are identical. Every query still counts as a
baseline evaluation; caching saves physical simulation work, not solver budget.
"""
from concurrent.futures import Future
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import threading

from analog_design.metrics import score
from analog_design.simulator import ROOT, digest, evaluate, parameters_for


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def evaluator_hashes():
    paths = ["analog_design/simulator.py", "analog_design/metrics.py", "analog_design/crosscheck.py",
             "analog_design/episode.py", "benchmark/simulation.py", "dependencies.lock.json"]
    return {path: digest(ROOT / path) for path in paths}


def include_closure(paths):
    """Resolve literal SPICE includes, conservatively including all library sections.

    The controlled evaluator uses file-relative includes. Dynamic include names
    are rejected rather than producing an incomplete physical identity.
    """
    pending = [Path(path).absolute() for path in paths]
    found = set()
    parsed = set()
    while pending:
        path = pending.pop()
        if path in found:
            continue
        if not path.is_file():
            raise FileNotFoundError(f"Missing SPICE dependency: {path}")
        found.add(path)
        if path.resolve() in parsed:
            continue
        parsed.add(path.resolve())
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("*"):
                continue
            directive = stripped.split(None, 1)[0].lower()
            if directive not in {".include", ".inc", ".lib"}:
                continue
            tokens = shlex.split(stripped, comments=False)
            if directive == ".lib" and len(tokens) == 2:
                continue  # A library section declaration, not an external file.
            if len(tokens) < 2:
                raise ValueError(f"Malformed SPICE include in {path}: {stripped}")
            filename = tokens[1]
            if any(char in filename for char in "$~{}"):
                raise ValueError(f"Dynamic SPICE include is unsupported: {filename}")
            child = Path(filename)
            if not child.is_absolute():
                child = path.parent / child
            # absolute() preserves symlinks so retargeting is detectable later.
            pending.append(child.absolute())
    return sorted(found)


def _file_state(path):
    stat = path.stat()
    return (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


class _Snapshot:
    """Fail closed if a captured input changes during a cache object's lifetime."""
    def __init__(self):
        self.files = {}
        executable = shutil.which("ngspice")
        if not executable:
            raise FileNotFoundError("ngspice is not installed.")
        self.executable = Path(executable).absolute()
        self.version = subprocess.check_output([str(self.executable), "--version"],
                                               text=True, timeout=5).strip()
        self.capture([self.executable])

    def capture(self, paths):
        hashes = {}
        for path in paths:
            path = Path(path).absolute()
            if path not in self.files:
                before = _file_state(path)
                sha256 = digest(path)
                if before != _file_state(path):
                    raise RuntimeError(f"Input changed while hashing: {path}")
                self.files[path] = (before, sha256)
            hashes[str(path)] = self.files[path][1]
        return hashes

    def assert_unchanged(self, *, full_hash=False):
        current = shutil.which("ngspice")
        if current is None or Path(current).absolute() != self.executable:
            raise RuntimeError("ngspice executable changed during benchmark work.")
        for path, (state, sha256) in self.files.items():
            try:
                changed = _file_state(path) != state
                if full_hash:
                    changed = changed or digest(path) != sha256
            except OSError as error:
                raise RuntimeError(f"Benchmark input disappeared: {path}") from error
            if changed:
                raise RuntimeError(f"Benchmark input changed; create a new cache instance: {path}")
        if full_hash:
            version = subprocess.check_output([str(self.executable), "--version"],
                                              text=True, timeout=5).strip()
            if version != self.version:
                raise RuntimeError("ngspice version changed during benchmark work.")


def measurement_identity(task, *, snapshot=None, simulator_timeout=30):
    snapshot = snapshot or _Snapshot()
    fields = ("circuit_directory", "subcircuit", "conditions", "parameters", "ac", "transient")
    identity = {key: deepcopy(task[key]) for key in fields}
    directory = ROOT / task["circuit_directory"]
    corner = ROOT / ".deps/sky130_pdk/libs.tech/ngspice/corners" / (task["conditions"]["corner"] + ".spice")
    dependency_paths = include_closure([directory / "netlist.spice", directory / "reference.params", corner])
    identity["spice_dependency_sha256"] = snapshot.capture(dependency_paths)
    evaluator_paths = [ROOT / path for path in evaluator_hashes()]
    identity["evaluator_sha256"] = snapshot.capture(evaluator_paths)
    identity["runtime"] = {
        "executable": str(snapshot.executable),
        "executable_sha256": snapshot.files[snapshot.executable][1],
        "ngspice_version": snapshot.version,
        "python_version": sys.version,
        "platform": platform.platform(),
        "simulator_timeout_s": simulator_timeout,
    }
    identity["cache_format_version"] = 2
    snapshot.assert_unchanged()
    return identity


def rescore(result, constraints):
    result = deepcopy(result)
    if result["status"] != "ok":
        return result
    primary = score(result["metrics"], constraints)
    independent = score(result["independent_metrics"], constraints)
    result.update(primary)
    if primary["checks"] != independent["checks"]:
        result.update(status="failed", success=False, reward=-1.0,
                      error="The two measurement paths disagree on pass/fail for these targets.")
    return result


class MeasurementCache:
    def __init__(self, directory, *, simulator_timeout=30):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout = simulator_timeout
        if not math.isfinite(simulator_timeout) or simulator_timeout <= 0:
            raise ValueError("The simulator timeout must be finite and positive.")
        self.lock = threading.Lock()
        self.pending = {}
        self.identities = {}
        self.physical_evaluations = 0
        self.cache_hits = 0
        self.snapshot = _Snapshot()

    def assert_unchanged(self):
        """Call between discovery, selection, validation, and publication stages.

        Each measurement checks inode, size, timestamps, symlink targets and PATH;
        stage boundaries additionally rehash every captured input and check the
        simulator version. Input mutation aborts rather than reusing stale data.
        """
        with self.lock:
            self.snapshot.assert_unchanged(full_hash=True)

    def measure(self, task, parameters):
        task = deepcopy(task)
        parameters = parameters_for(task, parameters)
        # Constraint-free measurements use permissive sentinel targets to avoid
        # caching a target-dependent pass/fail disagreement as a physical failure.
        signature = canonical_hash({"task": {key: task[key] for key in
                                    ("circuit_directory", "subcircuit", "conditions", "parameters", "ac", "transient")},
                                    "simulator_timeout_s": self.timeout})
        with self.lock:
            self.snapshot.assert_unchanged()
            identity = self.identities.get(signature)
            if identity is None:
                identity = measurement_identity(task, snapshot=self.snapshot, simulator_timeout=self.timeout)
                self.identities[signature] = identity
        key = canonical_hash({"identity": identity, "parameters": parameters})
        run = self.directory / key[:2] / key
        with self.lock:
            pending = self.pending.get(key)
            owner = pending is None
            if owner:
                pending = self.pending[key] = Future()
            else:
                self.cache_hits += 1
        if owner:
            try:
                result_file = run / "result.json"
                if result_file.is_file():
                    entry = json.loads((run / "cache_entry.json").read_text())
                    if (entry.get("measurement_key") != key or entry.get("identity") != identity
                            or entry.get("result_sha256") != digest(result_file)):
                        raise ValueError(f"Cached measurement identity or result hash disagrees: {run}")
                    result = json.loads(result_file.read_text())
                    if result.get("parameters") != parameters:
                        raise ValueError(f"Cached measurement parameters disagree: {run}")
                    with self.lock:
                        self.cache_hits += 1
                else:
                    probe = deepcopy(task)
                    probe["id"] = "measurement_" + key[:20]
                    probe["initial_parameters"] = parameters
                    probe["constraints"] = {"power_w": {"max": 1e6}}
                    task_path = self.directory / "inputs" / (key + ".json")
                    save_json(task_path, probe)
                    result = evaluate(task_path, parameters, run, self.timeout)
                    archive = {}
                    for path in sorted(run.glob("*.tsv")):
                        archive[path.name] = digest(path)
                        with path.open("rb") as original, gzip.open(str(path) + ".gz", "wb", compresslevel=6) as compressed:
                            shutil.copyfileobj(original, compressed)
                        path.unlink()
                    save_json(run / "archive.json", {"format": "gzip", "uncompressed_sha256": archive})
                    with self.lock:
                        self.snapshot.assert_unchanged()
                    save_json(run / "cache_entry.json", {"measurement_key": key, "identity": identity,
                                                         "result_sha256": digest(result_file)})
                    with self.lock:
                        self.physical_evaluations += 1
                result["measurement_key"] = key
                result["measurement_directory"] = str(run.relative_to(ROOT))
                pending.set_result(result)
            except BaseException as error:
                pending.set_exception(error)
                raise
        result = deepcopy(pending.result())
        with self.lock:
            self.snapshot.assert_unchanged()
        return result

    def evaluate(self, task, parameters):
        return rescore(self.measure(task, parameters), task["constraints"])
