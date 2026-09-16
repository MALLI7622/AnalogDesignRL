"""Explicit, expiring authorization for a bounded, unqualified research pilot.

The CLI only prepares unauthorized drafts. It never creates normal training
approvals, and a draft must not be marked authorized without explicit user approval.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from analog_design.model_clients import strict_json
from analog_design.simulator import ROOT, digest
from training.catalog import load_catalog


BOUND_CODE = (
    "analog_design/simulator.py", "analog_design/metrics.py", "analog_design/crosscheck.py",
    "analog_design/episode.py", "analog_design/qualification.py", "dependencies.lock.json",
    "training/catalog.py", "training/client.py", "training/worker.py", "training/train.py",
    "training/tunix_env.py", "training/research_pilot.py",
)
LIMIT_NAMES = ("max_episodes", "max_evaluations_per_episode", "max_total_evaluations")


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Pilot timestamps must be explicit timezone-aware strings")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Pilot timestamps must include a timezone")
    return result


def _path(name):
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("Pilot input paths must be project-relative")
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("Pilot input path escapes the project")
    return path


def _relative(path):
    return str(Path(path).resolve().relative_to(ROOT.resolve()))


def _read(path):
    before = digest(path)
    record = strict_json(Path(path).read_text())
    if digest(path) != before:
        raise ValueError("Pilot input changed while being read")
    return record, before


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(name + " must be a positive integer")
    return value


def _shape(record, *, require_authorized=True):
    if (type(record.get("schema_version")) is not int or record["schema_version"] != 1
            or record.get("kind") != "bounded_research_pilot"
            or record.get("qualified_training_approved") is not False
            or record.get("independent_expert_review") != "pending"):
        raise ValueError("Invalid research-pilot schema or qualification claims")
    if type(record.get("authorized")) is not bool:
        raise ValueError("Pilot authorized must be a boolean")
    if require_authorized and record["authorized"] is not True:
        raise ValueError("Research pilot is not explicitly authorized; this manifest is a draft")
    if _timestamp(record["expires_utc"]) <= _now():
        raise ValueError("Research-pilot authorization has expired")
    if _timestamp(record["created_utc"]) >= _timestamp(record["expires_utc"]):
        raise ValueError("Pilot expiry must follow its creation")
    if _timestamp(record["expires_utc"]) - _timestamp(record["created_utc"]) > timedelta(hours=24):
        raise ValueError("Pilot authorization cannot last more than twenty-four hours")
    if record["authorized"]:
        authorization = record.get("authorization", {})
        if any(not isinstance(authorization.get(key), str) or not authorization[key].strip()
               for key in ("authorized_by", "authorized_utc", "basis")):
            raise ValueError("Pilot authorization must record who, when, and its explicit user-request basis")
        if not _timestamp(record["created_utc"]) <= _timestamp(authorization["authorized_utc"]) <= _now():
            raise ValueError("Pilot authorization time is outside its valid interval")
    limits = record["limits"]
    if set(limits) != set(LIMIT_NAMES):
        raise ValueError("Pilot limits are incomplete")
    for key in LIMIT_NAMES:
        _positive(limits[key], key)
    if limits["max_evaluations_per_episode"] > 30:
        raise ValueError("Pilot cannot extend the task's thirty-evaluation contract")
    if limits["max_total_evaluations"] > limits["max_episodes"] * limits["max_evaluations_per_episode"]:
        raise ValueError("Pilot total-evaluation limit exceeds its episode limits")


def _configuration(config, limits):
    for key in ("max_updates", "num_generations", "max_episode_steps"):
        _positive(config[key], key)
    if config["max_episode_steps"] > limits["max_evaluations_per_episode"]:
        raise ValueError("Trainer attempt limit exceeds the authorized pilot limit")
    training_episodes = config["max_updates"] * config["num_generations"]
    if training_episodes > limits["max_episodes"]:
        raise ValueError("Training alone exceeds the authorized episode limit")
    if training_episodes * config["max_episode_steps"] > limits["max_total_evaluations"]:
        raise ValueError("Training alone exceeds the authorized evaluation limit")


def _catalog_bindings(catalog_path):
    manifest, tasks = load_catalog(catalog_path, training=False)
    splits = {entry["split"] for entry in tasks.values()}
    if splits != {"train", "validation"}:
        raise ValueError("Research pilot requires train/validation only; test and smoke tasks are forbidden")
    files = set(BOUND_CODE)
    for entry in tasks.values():
        files.add(_relative(entry["task_path"]))
        task, _ = _read(entry["task_path"])
        for basename in ("netlist.spice", "reference.params"):
            files.add(_relative(_path(task["circuit_directory"] + "/" + basename)))
    return manifest, tasks, files


def prepare_manifest(catalog_path, config_path, evidence_paths, *, max_episodes=20,
                     max_evaluations_per_episode=4, max_total_evaluations=80, expires_hours=4):
    """Build a reviewable draft; authorization is deliberately unavailable here."""
    if isinstance(expires_hours, bool) or not isinstance(expires_hours, (int, float)) or not 0 < expires_hours <= 24:
        raise ValueError("Pilot expiry must be positive and no more than twenty-four hours")
    _, tasks, files = _catalog_bindings(catalog_path)
    config, config_hash = _read(config_path)
    if not evidence_paths:
        raise ValueError("A pilot draft must identify its supporting evidence")
    evidence = {_relative(path): digest(path) for path in evidence_paths}
    created = _now()
    record = {"schema_version": 1, "kind": "bounded_research_pilot", "authorized": False,
              "authorization": {"authorized_by": None, "authorized_utc": None, "basis": None},
              "created_utc": created.isoformat(), "expires_utc": (created + timedelta(hours=expires_hours)).isoformat(),
              "qualified_training_approved": False, "independent_expert_review": "pending",
              "catalog": {"path": _relative(catalog_path), "sha256": digest(catalog_path)},
              "config": {"path": _relative(config_path), "sha256": config_hash},
              "task_sha256": {name: entry["sha256"] for name, entry in sorted(tasks.items())},
              "inputs_sha256": {name: digest(_path(name)) for name in sorted(files)},
              "evidence_sha256": evidence,
              "limits": {"max_episodes": max_episodes, "max_evaluations_per_episode": max_evaluations_per_episode,
                         "max_total_evaluations": max_total_evaluations},
              "scope": "User-authorized runtime research only, pending explicit authorization. No independent expert, production, or normal training qualification. One worker invocation; no automatic restart or quota reset."}
    _shape(record, require_authorized=False)
    _configuration(config, record["limits"])
    return record


def _bound_files(record):
    if any(name in record["inputs_sha256"] and record["inputs_sha256"][name] != checksum
           for name, checksum in record["evidence_sha256"].items()):
        raise ValueError("Conflicting pilot evidence and input bindings")
    bindings = {**record["inputs_sha256"], **record["evidence_sha256"]}
    for key in ("catalog", "config"):
        binding = record[key]
        if binding["path"] in bindings and bindings[binding["path"]] != binding["sha256"]:
            raise ValueError("Conflicting pilot file bindings")
        bindings[binding["path"]] = binding["sha256"]
    return bindings


def _check_files(record):
    for name, expected in _bound_files(record).items():
        if not isinstance(expected, str) or len(expected) != 64 or digest(_path(name)) != expected:
            raise ValueError("Pilot input changed or has a missing hash: " + name)


def _public(record, manifest_hash):
    return {"research_pilot": True, "pilot_manifest_sha256": manifest_hash,
            "independent_expert_review": "pending", "qualified_training_approved": False,
            "pilot_limits": dict(record["limits"]), "pilot_expires_utc": record["expires_utc"]}


class PilotAuthorization:
    """Fail-closed worker binding with durable, single-invocation usage counts."""
    def __init__(self, manifest_path, catalog_path):
        self.path = Path(manifest_path).resolve()
        self.record, self.sha256 = _read(self.path)
        _shape(self.record)
        if _path(self.record["catalog"]["path"]) != Path(catalog_path).resolve():
            raise ValueError("Pilot manifest is bound to a different catalog")
        _, tasks, required = _catalog_bindings(catalog_path)
        if self.record["task_sha256"] != {name: entry["sha256"] for name, entry in tasks.items()}:
            raise ValueError("Pilot task bindings differ from the exact catalog")
        if not required <= set(self.record["inputs_sha256"]):
            raise ValueError("Pilot manifest omits required evaluator, task, or circuit hashes")
        if not self.record["evidence_sha256"]:
            raise ValueError("Pilot manifest omits its supporting evidence")
        _configuration(_read(_path(self.record["config"]["path"]))[0], self.record["limits"])
        self.assert_current()
        self.usage_path = None
        self.episodes = self.evaluations = 0

    @property
    def limits(self):
        return self.record["limits"]

    def public(self):
        return _public(self.record, self.sha256)

    def assert_current(self):
        if digest(self.path) != self.sha256:
            raise ValueError("Research-pilot authorization changed while running")
        _shape(self.record)
        _check_files(self.record)

    def claim_worker(self, output):
        self.assert_current()
        self.usage_path = ROOT / "runs/research_pilot_usage" / (self.sha256 + ".json")
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.output = str(Path(output).resolve())
        try:
            with self.usage_path.open("x") as stream:
                json.dump(self._usage(), stream, indent=2)
                stream.write("\n")
        except FileExistsError as exc:
            raise ValueError("Pilot authorization was already used; automatic worker restart or quota reset is forbidden") from exc

    def _usage(self):
        return {**self.public(), "worker_output": self.output, "episodes_reserved": self.episodes,
                "evaluations_reserved": self.evaluations, "updated_utc": _now().isoformat()}

    def reserve(self, kind):
        # The caller holds the worker lock. Reserve before work, including failures.
        self.assert_current()
        if self.usage_path is None:
            raise ValueError("Pilot worker has not claimed its authorization")
        field, limit = ("episodes", "max_episodes") if kind == "episode" else ("evaluations", "max_total_evaluations")
        if kind not in {"episode", "evaluation"}:
            raise ValueError("Unknown pilot resource")
        if getattr(self, field) >= self.limits[limit]:
            raise ValueError("Research-pilot " + kind + " limit exhausted")
        setattr(self, field, getattr(self, field) + 1)
        temporary = self.usage_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._usage(), indent=2) + "\n")
        temporary.replace(self.usage_path)


def validate_trainer_manifest(path, config, catalog):
    """Validate authorization/config/worker identity without opening references."""
    record, checksum = _read(path)
    _shape(record)
    saved_config, config_hash = _read(_path(record["config"]["path"]))
    if config_hash != record["config"]["sha256"] or saved_config != config:
        raise ValueError("Trainer config differs from the authorized pilot")
    _configuration(config, record["limits"])
    expected = _public(record, checksum)
    if catalog.get("mode") != "research-pilot" or any(catalog.get(key) != value for key, value in expected.items()):
        raise ValueError("Worker does not match the authorized research-pilot manifest")
    if catalog.get("catalog_sha256") != record["catalog"]["sha256"]:
        raise ValueError("Worker catalog differs from the authorized pilot")
    rows = catalog["tasks"]
    if ({row["split"] for row in rows} != {"train", "validation"}
            or len(rows) != len(record["task_sha256"])
            or {row["id"]: row["sha256"] for row in rows} != record["task_sha256"]):
        raise ValueError("Worker tasks differ from the authorized train/validation catalog")
    return {**expected, "authorization": dict(record["authorization"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--evidence", required=True, nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-episodes", type=int, default=20)
    parser.add_argument("--max-evaluations-per-episode", type=int, default=4)
    parser.add_argument("--max-total-evaluations", type=int, default=80)
    parser.add_argument("--expires-hours", type=float, default=4)
    args = parser.parse_args()
    record = prepare_manifest(args.catalog, args.config, args.evidence, max_episodes=args.max_episodes,
                              max_evaluations_per_episode=args.max_evaluations_per_episode,
                              max_total_evaluations=args.max_total_evaluations, expires_hours=args.expires_hours)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(record, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(f"Unauthorized research-pilot draft: {args.output}")


if __name__ == "__main__":
    main()
