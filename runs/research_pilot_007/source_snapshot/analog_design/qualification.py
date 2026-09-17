"""Require an explicit, current review record for training-mode episodes."""
import json
from pathlib import Path

from .simulator import ROOT, digest


def require_training_approval(task_path):
    task_path = Path(task_path).resolve()
    task = json.loads(task_path.read_text())
    record = json.loads((ROOT / "verification/qualification.json").read_text())
    approval = record["approved_tasks"].get(task["id"])
    if not approval:
        raise RuntimeError("Training is not approved for this task. See verification/qualification.json and the verification report.")
    if not approval.get("reviewer") or approval.get("task_sha256") != digest(task_path):
        raise RuntimeError("Training approval is incomplete or refers to a different task version.")
    expected = approval.get("reviewed_files_sha256", {})
    required = ["analog_design/simulator.py", "analog_design/metrics.py", "analog_design/crosscheck.py",
                "analog_design/episode.py", "dependencies.lock.json",
                task["circuit_directory"] + "/netlist.spice", task["circuit_directory"] + "/reference.params"]
    if any(name not in expected or expected[name] != digest(ROOT / name) for name in required):
        raise RuntimeError("Reviewed evaluator or circuit files have changed; training approval is stale.")
