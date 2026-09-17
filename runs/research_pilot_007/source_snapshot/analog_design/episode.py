"""A budgeted, JSON-based sizing loop for a trusted evaluator process."""
import json
from pathlib import Path

from .simulator import evaluate, parameters_for
from .qualification import require_training_approval


class Episode:
    def __init__(self, task_path, output_directory, timeout_s=30, *, for_training=False):
        if for_training:
            require_training_approval(task_path)
        self.task = json.loads(Path(task_path).read_text())
        self.budget = self.task["max_evaluations"]
        if isinstance(self.budget, bool) or not isinstance(self.budget, int) or self.budget < 1:
            raise ValueError("max_evaluations must be a positive integer.")
        self.parameters = parameters_for(self.task, {})
        self.directory = Path(output_directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.task_path = self.directory / "task.json"
        self.task_path.write_text(json.dumps(self.task, indent=2) + "\n")
        self.timeout_s = timeout_s
        self.history = []
        self.done = False

    def specification(self):
        """Agent-visible requirements; passing references are not included."""
        fields = ("id", "conditions", "parameters", "constraints", "ac", "transient")
        specification = {**{key: self.task[key] for key in fields},
                         "current_parameters": dict(self.parameters),
                         "evaluations_remaining": self.budget - len(self.history)}
        return json.loads(json.dumps(specification))

    def step(self, action):
        """Apply absolute values to the current design; every attempt spends a slot."""
        if self.done:
            raise RuntimeError("Episode ended: a design passed or the budget was exhausted.")
        candidate = {**self.parameters, **action} if isinstance(action, dict) else action
        run_dir = self.directory / f"evaluation_{len(self.history) + 1:03d}"
        result = evaluate(self.task_path, candidate, run_dir, self.timeout_s)
        if "parameters" in result:
            self.parameters = result["parameters"]
        remaining = self.budget - len(self.history) - 1
        terminated = result["success"]
        truncated = remaining == 0 and not terminated
        self.done = terminated or truncated
        observation = {key: result[key] for key in
                       ("task_id", "status", "success", "reward", "metrics", "checks",
                        "simulator_invocations", "elapsed_s")}
        observation.update(parameters=dict(self.parameters), evaluations_remaining=remaining,
                           terminated=terminated, truncated=truncated)
        if "error" in result:
            observation["error"] = result["error"]
        # Preserve malformed actions without emitting non-standard JSON NaN/Infinity.
        try:
            recorded_action = json.loads(json.dumps(action, allow_nan=False))
        except (TypeError, ValueError):
            recorded_action = repr(action)
        self.history.append({"evaluation": len(self.history) + 1, "action": recorded_action,
                             "run_directory": run_dir.name, **observation})
        summary = {"task_id": self.task["id"], "max_evaluations": self.budget,
                   "evaluations_used": len(self.history),
                   "simulator_invocations": sum(r["simulator_invocations"] for r in self.history),
                   "success": terminated, "done": self.done, "trajectory": self.history}
        (self.directory / "episode.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
        return observation
