"""Export a release's tasks for the unchanged numeric training worker.

Use this after the release's separate verification gates have passed. This
adapter verifies index/task identity and split integrity, then checks the result
with ``training.catalog.load_catalog(training=False)``. It does not simulate,
read solutions, grant training approval, or start a worker.

The existing ``training.worker`` constructs the base ``Episode`` and exposes its
numeric specification. The symbolic context supplied by ``BenchmarkEpisode``
requires a separate worker integration; exporting this catalog does not activate
that wrapper. Training mode still requires genuine per-task qualification.
"""
import argparse
import json
from pathlib import Path
import re

from analog_design.model_clients import strict_json
from analog_design.simulator import ROOT, digest
from training.catalog import group_key, load_catalog


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("Invalid task, topology, or family identifier.")
    return value


def export_worker_catalog(index_path, output_path=None):
    """Write a NEW catalog with the release's exact family split assignments.

    Task paths become project-relative, as required by the existing loader.
    Hash and compatibility checks here do not replace fresh release verification
    or the existing training approval gate. No reference path is followed.
    """
    root = ROOT.resolve()
    index_path = Path(index_path).resolve()
    output = Path(output_path).resolve() if output_path is not None else index_path.parent / "worker_catalog.json"
    if not index_path.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError("Worker catalog inputs and output must be within the project.")
    if output.exists():
        raise FileExistsError("Worker catalog output must be new.")
    hashes = {index_path: digest(index_path)}
    index = strict_json(index_path.read_text())
    rows = index.get("tasks")
    if index.get("schema_version") != 1 or not isinstance(rows, list) or not rows:
        raise ValueError("A version-one nonempty release index is required.")
    if index.get("split_policy") != "topology_family":
        raise ValueError("The release must declare topology_family split policy.")
    if "task_count" in index and (type(index["task_count"]) is not int or index["task_count"] != len(rows)):
        raise ValueError("Release task count disagrees with its index.")
    seen, families, topologies, groups, entries = set(), {}, {}, {}, []
    for row in rows:
        name, topology, family = [_identifier(row[field]) for field in ("id", "topology", "topology_family")]
        if name in seen:
            raise ValueError("Duplicate release task ID.")
        seen.add(name)
        if row.get("path") != f"tasks/{name}.json":
            raise ValueError("Release task path must match its ID.")
        task_path = (index_path.parent / row["path"]).resolve()
        if not task_path.is_relative_to(index_path.parent) or not task_path.is_relative_to(root):
            raise ValueError("Release task path escapes its bundle.")
        actual = digest(task_path)
        if actual != row.get("sha256"):
            raise ValueError("Release task content hash disagrees: " + name)
        hashes[task_path] = actual
        task = strict_json(task_path.read_text())
        if task.get("id") != name:
            raise ValueError("Release task ID disagrees with its index.")
        if type(task.get("max_evaluations")) is not int or task["max_evaluations"] != 30:
            raise ValueError("Benchmark tasks require a thirty-evaluation budget.")
        split = row.get("split")
        if split not in {"train", "validation", "test"}:
            raise ValueError("Release split must be train, validation, or test.")
        if family in families and families[family] != split:
            raise ValueError("A topology family occurs in different splits.")
        if topology in topologies and topologies[topology] != family:
            raise ValueError("A topology is assigned to multiple families.")
        context = task.get("design_context", {})
        if (context.get("topology", topology) != topology
                or context.get("topology_family", context.get("family", family)) != family):
            raise ValueError("Task context disagrees with its topology family.")
        circuit = task.get("circuit_directory")
        if not isinstance(circuit, str) or not circuit or Path(circuit).is_absolute():
            raise ValueError("Task circuit directory must be project-relative.")
        if not (root / circuit).resolve().is_relative_to(root):
            raise ValueError("Task circuit directory escapes the project.")
        group = group_key(task, "topology")
        if group in groups and groups[group] != split:
            raise ValueError("The worker's topology group occurs in different splits.")
        families[family], topologies[topology], groups[group] = split, family, split
        entries.append({"id": name, "path": str(task_path.relative_to(root)), "sha256": actual,
                        "group": group, "split": split, "topology": topology,
                        "topology_family": family, "difficulty": row.get("difficulty")})
    manifest = {"schema_version": 1, "split_policy": "topology", "tasks": entries,
                "source_index": str(index_path.relative_to(root)), "source_index_sha256": hashes[index_path],
                "source_split_policy": "topology_family", "family_holdout_preserved": True,
                "family_splits": dict(sorted(families.items())),
                "compatibility": {"loader": "training.catalog.load_catalog(training=False)",
                    "worker": "training.worker", "observation": "base Episode numeric specification",
                    "benchmark_episode_context_active": False},
                "validation_scope": "Task hashes, family splits, and numeric worker catalog compatibility; no fresh simulation.",
                "training_approval": "Not granted by export; the existing per-task qualification gate remains required."}
    for path, expected in hashes.items():
        if digest(path) != expected:
            raise ValueError("Release input changed during catalog export: " + str(path))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write("\n")
    loaded, records = load_catalog(output, training=False)
    if loaded != manifest or len(records) != len(entries):
        raise RuntimeError("Worker catalog compatibility validation disagrees with the exported entries.")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="New output file; defaults to worker_catalog.json beside the index.")
    args = parser.parse_args()
    print(export_worker_catalog(args.index, args.output))


if __name__ == "__main__":
    main()
