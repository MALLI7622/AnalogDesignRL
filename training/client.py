"""Learner-side client. Network failures abort rollouts; they are never rewards."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from analog_design.model_clients import strict_json

SYSTEM_PROMPT = """Size the analog circuit to satisfy every fixed constraint using simulator feedback.
Reply with ONLY a JSON object mapping allowed parameter names to numeric absolute values.
Use the units in the specification. Omitted parameters keep their current value; {} evaluates the current design.
Every response spends one evaluation, including invalid responses. You cannot change requirements or circuit connections.
Stop when the environment ends. Do not include explanations, Markdown, executable code, or tool calls."""


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
    fields = ("status", "success", "reward", "metrics", "parameters", "evaluations_remaining", "error")
    if "constraints" in observation:
        public = observation
    else:
        public = {k: observation[k] for k in fields if k in observation}
        public["failed_requirements"] = [key for key, passed in observation.get("checks", {}).items()
                                         if not passed]
    return json.dumps(public, separators=(",", ":"), allow_nan=False)


class RemoteEpisode:
    def __init__(self, client, task_id, *, max_steps, required_mode="training"):
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.client, self.task_id, self.max_steps, self.required_mode = client, task_id, max_steps, required_mode
        self.key = None
        self.steps, self.previous_score = 0, 0.0
        self.done = False

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
        result = self.client.request("/step", {"episode_id": self.key, "step": self.steps + 1, "action": action})
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
        return observation_text(result), reward, self.done, info

    def close(self):
        if self.key is not None:
            self.client.request("/close", {"episode_id": self.key})
            self.key = None
