"""Freeze tasks and keep related requirements out of different dataset splits."""
import argparse
import hashlib
import json
from pathlib import Path

from analog_design.simulator import ROOT, digest
from analog_design.qualification import require_training_approval


def group_key(task, policy):
    fields = ["circuit_directory"]
    if policy == "requirements":
        fields += ["conditions", "constraints", "ac", "transient"]
    elif policy != "topology":
        raise ValueError("split_policy must be topology or requirements")
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(child) for key, child in value.items()}
        if isinstance(value, list):
            return [normalize(child) for child in value]
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value == int(value):
            return int(value)
        return value
    data = normalize({key: task[key] for key in fields})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def load_catalog(path, *, training=False):
    manifest = json.loads(Path(path).read_text())
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported catalog version")
    entries, groups = {}, {}
    for entry in manifest["tasks"]:
        task_path = (ROOT / entry["path"]).resolve()
        if not task_path.is_relative_to(ROOT):
            raise ValueError("Catalog paths must be within the project")
        task = json.loads(task_path.read_text())
        task_id = task["id"]
        if task_id in entries or entry["id"] != task_id:
            raise ValueError("Duplicate or mismatched task ID")
        if digest(task_path) != entry["sha256"]:
            raise ValueError(f"Task changed since catalog was frozen: {task_id}")
        split = entry["split"]
        if split not in {"smoke", "train", "validation", "test"}:
            raise ValueError("Unknown split")
        group = group_key(task, manifest["split_policy"])
        if entry["group"] != group:
            raise ValueError("Task group does not match its circuit and requirements")
        if group in groups and groups[group] != split:
            raise ValueError("Related tasks occur in different splits")
        groups[group] = split
        if training:
            if split == "smoke":
                raise ValueError("Smoke tasks cannot be served in training mode")
            require_training_approval(task_path)
        entries[task_id] = {**entry, "task_path": task_path, "max_evaluations": task["max_evaluations"]}
    if not entries:
        raise ValueError("Empty task catalog")
    return manifest, entries


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--tasks", nargs="+", required=True)
    cli.add_argument("--output", required=True)
    cli.add_argument("--split-policy", choices=["topology", "requirements"], default="topology")
    args = cli.parse_args()
    entries = []
    for name in args.tasks:
        path = Path(name).resolve()
        task = json.loads(path.read_text())
        entries.append({"id": task["id"], "path": str(path.relative_to(ROOT)),
                        "sha256": digest(path), "group": group_key(task, args.split_policy), "split": "smoke"})
    manifest = {"schema_version": 1, "split_policy": args.split_policy, "tasks": entries}
    with Path(args.output).open("x") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    load_catalog(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
