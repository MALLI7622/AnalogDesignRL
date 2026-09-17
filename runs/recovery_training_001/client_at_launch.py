"""Learner-side client. Network failures abort rollouts; they are never rewards."""
import json
from datetime import datetime, timezone
from pathlib import Path
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from analog_design.model_clients import strict_json

SYSTEM_PROMPT = """Size the analog circuit to satisfy every fixed constraint using simulator feedback.
Reply with ONLY a JSON object mapping allowed parameter names to numeric absolute values.
Use the units in the specification. Omitted parameters keep their current value; {} evaluates the current design.
Every response spends one evaluation, including invalid responses. You cannot change requirements or circuit connections.
Stop when the environment ends. Do not include explanations, Markdown, executable code, or tool calls."""

RESPONSE_FORMAT = (
    'Return only a flat parameter-to-number JSON object, without Markdown fences. '
    'Do not repeat the task description, constraints, or measurements.'
)

EXPLORATION_INSTRUCTIONS = """On your first attempt, change at least one allowed parameter from its current value.
Keep every value within its stated bounds and keep integer parameters integral.
After feedback, choose a different candidate to address a failed requirement.
Do not repeat a previously evaluated design. Use only allowed parameter names.
Return only the parameter JSON; do not explain your reasoning."""

SINGLE_CHANGE_INSTRUCTIONS = """The starting design does not satisfy all requirements. Repeating its values will not solve the task.
Change exactly one allowed parameter from its current value. Use its exact name and choose a different value within that parameter's own bounds. For integer parameters, choose an integer.
Return only that changed parameter as a JSON object. After feedback, use the failed requirements to choose your next change."""

RECOVERY_INSTRUCTIONS = """Start with a modest change from the current value within that parameter's own bounds, rather than jumping to a bound.
If an evaluation fails, try a different candidate. Do not repeat the same parameter values after a failed evaluation.
Read action_valid, parameters_changed, measurement_success, failure_category, and failed_requirements in the feedback. A valid action can still fail measurement or circuit requirements."""


def system_prompt(variant="current"):
    if variant in {"current", "no_answer_example_v1"}:
        return SYSTEM_PROMPT
    if variant == "exploration_v1":
        return SYSTEM_PROMPT + "\n" + EXPLORATION_INSTRUCTIONS
    if variant == "single_change_v1":
        return SYSTEM_PROMPT + "\n" + SINGLE_CHANGE_INSTRUCTIONS
    if variant == "single_change_recovery_v1":
        return SYSTEM_PROMPT + "\n" + SINGLE_CHANGE_INSTRUCTIONS + "\n" + RECOVERY_INSTRUCTIONS
    raise ValueError("Unknown prompt variant")


class WorkerError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise WorkerError("Worker redirects are disabled")


class WorkerClient:
    def __init__(self, endpoint, token, timeout_s=150):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username
                or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Use a loopback endpoint, optionally through an SSH tunnel")
        if not token or len(token) < 24:
            raise ValueError("ANALOG_WORKER_TOKEN must contain at least 24 characters")
        self.endpoint, self.token, self.timeout_s = endpoint.rstrip("/"), token, timeout_s
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = Request(self.endpoint + path, data=data,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                result = strict_json(response.read(1024 * 1024).decode())
            if not isinstance(result, dict):
                raise WorkerError("Worker returned a non-object response")
            return result
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise WorkerError(f"Simulator service request failed: {type(exc).__name__}") from exc


def observation_text(observation):
    # Keep numerical feedback compact to leave space for multiple design attempts.
    fields = ("status", "success", "reward", "metrics", "parameters", "evaluations_remaining", "error",
              "action_valid", "parameters_changed", "measurement_success", "failure_category")
    if "constraints" in observation:
        public = dict(observation)
    else:
        public = {k: observation[k] for k in fields if k in observation}
        public["failed_requirements"] = [key for key, passed in observation.get("checks", {}).items()
                                         if not passed]
    return json.dumps(public, separators=(",", ":"), allow_nan=False)


def action_prompt(observation, variant="current"):
    """Render the public observation with a concrete, answer-free action example."""
    public = strict_json(observation)
    if variant in {"no_answer_example_v1", "single_change_v1", "single_change_recovery_v1"}:
        return ("Circuit task and feedback:\n" + observation + "\n\n" + RESPONSE_FORMAT
                + "\nChoose your next parameter values. Reply in exactly that flat JSON format, "
                  "starting with { and ending with }. No other text.")
    if variant == "exploration_v1":
        return ("Circuit task and feedback:\n" + observation + "\n\n" + RESPONSE_FORMAT
                + "\n" + EXPLORATION_INSTRUCTIONS)
    if variant != "current":
        raise ValueError("Unknown prompt variant")
    parameters = public.get("current_parameters", public.get("parameters", {}))
    example = json.dumps(parameters, separators=(",", ":"), allow_nan=False)
    return ("Circuit task and feedback:\n" + observation + "\n\n" + RESPONSE_FORMAT
            + "\nThe current parameter values in the required reply format are:\n" + example
            + "\nChoose your next parameter values. Reply in exactly that flat JSON format, "
              "starting with { and ending with }. No other text.")


def scalar_task_id(value):
    """Tunix microbatches wrap one task ID in an ndarray/list."""
    if hasattr(value, "tolist"):
        value = value.tolist()
    while isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str) or not value:
        raise ValueError("Expected exactly one nonempty string task ID")
    return value


class RemoteEpisode:
    def __init__(self, client, task_id, *, max_steps, required_mode="training", trajectory_directory=None):
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.client, self.task_id, self.max_steps, self.required_mode = client, scalar_task_id(task_id), max_steps, required_mode
        self.key = None
        self.steps, self.previous_score = 0, 0.0
        self.done = False
        self.trajectory_directory = Path(trajectory_directory) if trajectory_directory else None
        self.trajectory_path = None

    def _record(self, event, **fields):
        if self.trajectory_path is not None:
            record = {"event": event, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                      "task_id": self.task_id, "episode_id": self.key, **fields}
            with self.trajectory_path.open("a") as stream:
                stream.write(json.dumps(record, allow_nan=False) + "\n")

    def reset(self):
        self.close()
        result = self.client.request("/episodes", {"task_id": self.task_id})
        self.key = result["episode_id"]
        if result["mode"] != self.required_mode:
            self.close()
            raise WorkerError("Worker mode does not match the requested run")
        self.steps, self.previous_score, self.done = 0, 0.0, False
        spec = result["specification"]
        self.limit = min(spec["evaluations_remaining"], self.max_steps)
        spec["evaluations_remaining"] = self.limit
        if self.trajectory_directory is not None:
            self.trajectory_directory.mkdir(parents=True, exist_ok=True)
            self.trajectory_path = self.trajectory_directory / (uuid.uuid4().hex + ".jsonl")
            with self.trajectory_path.open("x"):
                pass
            self._record("initial", mode=self.required_mode, specification=spec)
        return observation_text(spec)

    def step(self, response):
        if self.key is None or self.done:
            raise WorkerError("Reset an unfinished episode before stepping")
        try:
            action = strict_json(response) if isinstance(response, str) and len(response) <= 8192 else None
            if not isinstance(action, dict):
                action = "invalid JSON action"
        except (ValueError, RecursionError):
            action = "invalid JSON action"
        self._record("action", step=self.steps + 1, response=response, parsed_action=action)
        try:
            result = self.client.request("/step", {"episode_id": self.key, "step": self.steps + 1, "action": action})
        except Exception as exc:
            self._record("error", step=self.steps + 1, error_type=type(exc).__name__)
            raise
        if action == "invalid JSON action":
            result["error"] = (
                "Your response was rejected before simulation: expected a raw JSON object. "
                "Remove Markdown code fences and all text outside the object. "
                "Duplicate keys and non-finite numbers are also invalid. This attempt spent one evaluation."
            )
        self.steps += 1
        self.done = bool(result["terminated"] or result["truncated"] or self.steps >= self.limit)
        result["evaluations_remaining"] = min(result["evaluations_remaining"], self.limit - self.steps)
        # Tunix sums step rewards. Differences telescope to the final verifier score,
        # even when an episode hits its context limit before its evaluation budget.
        score = float(result["reward"])
        reward = score - self.previous_score
        self.previous_score = score
        info = {"verifier_reward": score, "success": result["success"], "steps": self.steps,
                "simulator_invocations": result["simulator_invocations"], "elapsed_s": result["elapsed_s"]}
        self._record("feedback", step=self.steps, observation=result, reward_delta=reward, done=self.done)
        return observation_text(result), reward, self.done, info

    def close(self):
        if self.key is not None:
            self.client.request("/close", {"episode_id": self.key})
            self._record("closed", steps=self.steps, final_score=self.previous_score, done=self.done)
            self.key = None
            self.trajectory_path = None
