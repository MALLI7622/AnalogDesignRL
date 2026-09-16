"""Stage the released train/validation tasks as a narrow repository overlay.

Copy the staged ``datasets`` directory into the same path of a remote checkout.
The executable task bytes and their project-relative paths are unchanged. This
does not copy test tasks, private evidence, circuit dependencies, or approvals;
it does not start a worker or grant permission through the training gate.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analog_design.model_clients import strict_json
from benchmark.learner import public_specification
from training.catalog import group_key, load_catalog


EXPECTED_COUNTS = {"train": 174, "validation": 21, "test": 55}
INCLUDED_SPLITS = {"train", "validation"}


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def prepare_training_bundle(index_path, catalog_path, output):
    """Validate the complete release and write a NEW 195-task staging folder.

    The source index is read for split/hash bindings, never copied. No reference
    path is followed. The manifest binds the two source catalogs and the 195
    included task files; it does not list held-out task identities or answers.
    """
    root = ROOT.resolve()
    index_path, catalog_path, output = (Path(p).resolve() for p in (index_path, catalog_path, output))
    if output.exists():
        raise FileExistsError("Staging output must be a new directory.")
    if not index_path.is_relative_to(root) or not catalog_path.is_relative_to(root):
        raise ValueError("Source index and catalog must be inside this project.")
    bundle = index_path.parent
    relative_bundle = bundle.relative_to(root)
    if len(relative_bundle.parts) != 2 or relative_bundle.parts[0] != "datasets":
        raise ValueError("Source release must be directly under datasets/.")
    if output.is_relative_to(bundle):
        raise ValueError("Staging output must not modify the immutable source release.")
    captured = {}

    def read(path, expected=None):
        content = path.read_bytes()
        actual = _sha(content)
        if expected is not None and expected != actual:
            raise ValueError("Source content hash disagrees: " + str(path))
        captured[path] = actual
        return strict_json(content.decode()), content

    def unchanged():
        for path, expected in captured.items():
            if _sha(path.read_bytes()) != expected:
                raise ValueError("Source changed during staging: " + str(path))

    index, _ = read(index_path)
    source, _ = read(catalog_path)
    rows = index.get("tasks")
    if (index.get("schema_version") != 1 or index.get("split_policy") != "topology_family"
            or not isinstance(rows, list) or len(rows) != 250 or index.get("task_count") != 250):
        raise ValueError("Expected the complete 250-task topology-family release index.")
    if Counter(row.get("split") for row in rows) != EXPECTED_COUNTS:
        raise ValueError("Release split counts must be 174 train, 21 validation, and 55 test.")
    if (source.get("schema_version") != 1 or source.get("split_policy") != "topology"
            or source.get("source_index") != str(index_path.relative_to(root))
            or source.get("source_index_sha256") != captured[index_path]
            or source.get("source_split_policy") != "topology_family"
            or source.get("family_holdout_preserved") is not True):
        raise ValueError("Worker catalog is not bound to this release and family split policy.")
    # Use the existing catalog's integrity checks without invoking qualification.
    loaded, records = load_catalog(catalog_path, training=False)
    if loaded != source or len(records) != 250:
        raise ValueError("Worker catalog changed or does not contain the complete release.")
    seen, families, topologies, entries, payloads = set(), {}, {}, [], {}
    for row in rows:
        name, topology, family = (row.get(k) for k in ("id", "topology", "topology_family"))
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v)
               for v in (name, topology, family)) or name in seen:
            raise ValueError("Release task identities must be valid and unique.")
        seen.add(name)
        if row.get("path") != f"tasks/{name}.json":
            raise ValueError("Release task path must match its ID.")
        split = row["split"]
        if family in families and families[family] != split:
            raise ValueError("A topology family occurs in different splits.")
        if topology in topologies and topologies[topology] != family:
            raise ValueError("A topology occurs in different families.")
        families[family], topologies[topology] = split, family
        task_path = bundle / row["path"]
        relative = str(task_path.relative_to(root))
        if task_path.resolve() != task_path:
            raise ValueError("Release task paths must not follow symlinks.")
        entry = records.get(name)
        if not entry or any(entry.get(key) != value for key, value in {
                "path": relative, "sha256": row.get("sha256"), "split": split,
                "topology": topology, "topology_family": family, "difficulty": row.get("difficulty")}.items()):
            raise ValueError("Worker catalog entry disagrees with the release: " + name)
        if split not in INCLUDED_SPLITS:
            continue
        task, content = read(task_path, row["sha256"])
        if task.get("id") != name or task.get("max_evaluations") != 30:
            raise ValueError("Task identity or evaluation budget disagrees with the release.")
        public = public_specification(task)
        # Reject, rather than silently copy, metadata the public whitelist drops.
        executable = {**public, "initial_parameters": public["current_parameters"],
                      "circuit_directory": task["circuit_directory"], "subcircuit": task["subcircuit"]}
        del executable["current_parameters"], executable["evaluations_remaining"]
        if task != executable:
            raise ValueError("Executable task contains unsupported non-public fields: " + name)
        context = task.get("design_context", {})
        if (context.get("topology", topology) != topology
                or context.get("topology_family", family) != family):
            raise ValueError("Task context disagrees with its release topology family.")
        circuit = task["circuit_directory"]
        if not isinstance(circuit, str) or Path(circuit).is_absolute() or not (root / circuit).resolve().is_relative_to(root):
            raise ValueError("Task circuit directory must stay within the remote project.")
        entries.append({"id": name, "path": relative, "sha256": row["sha256"],
                        "group": group_key(task, "topology"), "split": split, "topology": topology,
                        "topology_family": family, "difficulty": row.get("difficulty")})
        payloads[relative] = content
    if seen != set(records) or source.get("family_splits") != families:
        raise ValueError("Worker catalog identities or family assignments disagree with the release.")
    catalog_relative = str(relative_bundle / "train_validation_catalog.json")
    manifest_relative = str(relative_bundle / "training_bundle_manifest.json")
    filtered = {"schema_version": 1, "split_policy": "topology", "tasks": entries,
                "source_index": str(index_path.relative_to(root)), "source_index_sha256": captured[index_path],
                "source_catalog": str(catalog_path.relative_to(root)), "source_catalog_sha256": captured[catalog_path],
                "source_split_policy": "topology_family", "family_holdout_preserved": True,
                "family_splits": {key: split for key, split in families.items() if split in INCLUDED_SPLITS},
                "training_approval": "Not granted by staging; existing per-task qualification remains required."}
    payloads[catalog_relative] = _json_bytes(filtered)
    manifest = {"schema_version": 1, "status": "staged", "created_utc": datetime.now(timezone.utc).isoformat(),
                "scope": "Executable public train/validation tasks and filtered catalog; no training approval or simulation.",
                "task_count": len(entries), "split_counts": dict(Counter(row["split"] for row in entries)),
                "excluded_split_counts": {"test": EXPECTED_COUNTS["test"]},
                "catalog_path": catalog_relative, "catalog_sha256": _sha(payloads[catalog_relative]),
                "training_approved": False,
                "source_inputs_sha256": {str(p.relative_to(root)): h for p, h in captured.items()},
                "files": [{"path": p, "sha256": _sha(content), "bytes": len(content)}
                          for p, content in sorted(payloads.items())],
                "transfer": "Copy this overlay's datasets directory into the remote repository root. Source index/catalog paths are provenance only; those complete source files are not bundled."}
    unchanged()
    output.mkdir(parents=True, exist_ok=False)
    for relative, content in payloads.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
        if _sha(path.read_bytes()) != _sha(content):
            raise RuntimeError("Staged task/catalog bytes differ from the source payload.")
    # Paths remain project-relative, so this compatibility check uses source
    # tasks. Every staged counterpart has separately been checked byte-for-byte.
    actual, records = load_catalog(output / catalog_relative, training=False)
    if actual != filtered or len(records) != 195:
        raise RuntimeError("The filtered catalog failed existing loader compatibility.")
    unchanged()
    with (output / manifest_relative).open("xb") as stream:
        stream.write(_json_bytes(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=ROOT / "datasets/analog_benchmark_250_v1/index.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "datasets/analog_benchmark_250_v1/worker_catalog.json")
    parser.add_argument("--output", type=Path, required=True, help="New staging directory containing a repository overlay.")
    args = parser.parse_args()
    result = prepare_training_bundle(args.index, args.catalog, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "task_count": result["task_count"],
                      "split_counts": result["split_counts"], "catalog_path": result["catalog_path"]}))


if __name__ == "__main__":
    main()
