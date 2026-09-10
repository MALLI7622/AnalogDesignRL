"""Generate proposals through the signed-in Codex CLI, without API credentials."""
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from .model_clients import MAX_RESPONSE_BYTES, ProviderError, strict_json


# This worker only needs to return JSON from the supplied text.
DISABLED_FEATURES = (
    "shell_tool", "apps", "plugins", "hooks", "multi_agent", "code_mode_host",
    "browser_use", "browser_use_external", "in_app_browser", "computer_use",
    "image_generation", "view_image", "goals", "sleep_tool", "skill_search",
)


@dataclass
class CodexConfig:
    provider: str
    model: str
    timeout_s: float = 300
    executable: str = "codex"
    reasoning_effort: str = "medium"

    def __post_init__(self):
        if self.provider != "codex":
            raise ValueError("CodexConfig requires provider=codex.")
        if not isinstance(self.model, str) or not self.model.strip() or any(ord(c) < 32 for c in self.model):
            raise ValueError("Supply a model ID available in your Codex account.")
        if self.reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Unsupported Codex reasoning effort.")
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)) or not math.isfinite(self.timeout_s) or not 0 < self.timeout_s <= 1800:
            raise ValueError("Codex timeout_s must be positive, finite, and at most 1800 seconds.")
        if not isinstance(self.executable, str) or not self.executable.strip():
            raise ValueError("executable must name the Codex CLI binary.")


def codex_environment():
    # Reuse the CLI's saved login, never a leftover API key from a previous command.
    # CODEX_HOME is inherited unchanged so Codex manages its own authentication.
    return {key: value for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"}}


class CodexClient:
    mode = "live_model"

    def __init__(self, config):
        self.config = config
        self.executable = None
        self.version = None

    def describe(self):
        return {**asdict(self.config), "authentication": "saved ChatGPT login",
                "codex_version": self.version, "transport": "codex_exec"}

    def check_credentials(self):
        executable = shutil.which(self.config.executable)
        if not executable:
            raise ProviderError("Codex CLI is missing. Install it or set --codex-bin to its executable path.")
        try:
            env = codex_environment()
            version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15, env=env)
            help_result = subprocess.run([executable, "exec", "--help"], capture_output=True, text=True, timeout=15, env=env)
            required = ("--output-schema", "--output-last-message", "--ephemeral", "--ignore-user-config", "--json")
            if version.returncode or help_result.returncode or any(flag not in help_result.stdout for flag in required):
                raise ProviderError("Update Codex CLI: this provider needs exec, JSON schema output, ephemeral mode, and --ignore-user-config.")
            login = subprocess.run([executable, "login", "status"], capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.SubprocessError):
            raise ProviderError("Could not check Codex CLI. Run codex login status in your terminal.") from None
        if login.returncode or "chatgpt" not in (login.stdout + login.stderr).lower():
            raise ProviderError("Run codex login and sign in with ChatGPT. This provider requires a ChatGPT login, not an API key.")
        self.executable = executable
        self.version = version.stdout.strip()

    def command(self, directory):
        directory = Path(directory)
        command = [self.executable or self.config.executable, "exec", "--model", self.config.model,
                   "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                   "--ignore-user-config", "--color", "never", "--json",
                   "--cd", str(directory), "--output-schema", str(directory / "schema.json"),
                   "--output-last-message", str(directory / "proposal.json"),
                   "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                   "-c", "project_doc_max_bytes=0", "-c", "model_reasoning_effort=" + json.dumps(self.config.reasoning_effort)]
        for feature in DISABLED_FEATURES:
            command.extend(["--disable", feature])
        return [*command, "-"]

    def prepare(self, system, messages, schema):
        return {"transport": "codex_exec", "command": self.command("<temporary-directory>"),
                "body": {"instructions": system, "messages": messages, "schema": schema}}

    def complete(self, request):
        if self.executable is None:
            self.check_credentials()
        body = request["body"]
        prompt = ("You are generating one circuit-sizing task proposal. All source material is included below. "
                  "Do not use tools, read local files, or execute commands. Return only the requested JSON.\n\n" +
                  body["instructions"] + "\n\nConversation data:\n" +
                  json.dumps(body["messages"], indent=2, allow_nan=False))
        with tempfile.TemporaryDirectory(prefix="analog_codex_") as temporary:
            directory = Path(temporary).resolve()
            (directory / "schema.json").write_text(json.dumps(body["schema"], allow_nan=False))
            command = self.command(directory)
            with (directory / "events.jsonl").open("w+") as events, (directory / "stderr.txt").open("w+") as errors:
                process = None
                try:
                    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                               text=True, env=codex_environment(), start_new_session=True)
                    process.communicate(prompt, timeout=self.config.timeout_s)
                except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                    if process is not None:
                        # Kill the worker's process group so a timed-out proposal cannot continue.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.communicate()
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    raise ProviderError(f"Codex exceeded {self.config.timeout_s:g} seconds. Increase --request-timeout if needed.") from None
                except OSError:
                    raise ProviderError("Could not start Codex CLI.") from None
                events.seek(0)
                errors.seek(0)
                event_text = events.read(MAX_RESPONSE_BYTES + 1)
                stderr = errors.read(16000)
            raw = {"command": command, "exit_code": process.returncode, "events_jsonl": event_text, "stderr": stderr}
            if len(event_text) > MAX_RESPONSE_BYTES:
                raise ProviderError("Codex output exceeded the 8 MiB limit.", {"exit_code": process.returncode})
            completed = []
            failures = []
            for line in event_text.splitlines():
                try:
                    event = strict_json(line)
                except ValueError:
                    raise ProviderError("Codex returned malformed JSON events.", raw) from None
                if not isinstance(event, dict):
                    raise ProviderError("Codex returned an invalid event.", raw)
                if event.get("type") == "turn.completed":
                    completed.append(event)
                if event.get("type") in {"error", "turn.failed"}:
                    failures.append(event)
                item = event.get("item", {})
                if event.get("type", "").startswith("item."):
                    if not isinstance(item, dict):
                        raise ProviderError("Codex returned an invalid item event.", raw)
                    # item.error includes nonfatal CLI startup diagnostics (for example,
                    # Code Mode being unavailable because we deliberately disabled it).
                    # A failed turn is reported separately and remains a rejection.
                    if item.get("type") not in {"agent_message", "reasoning", "error"}:
                        raise ProviderError("Codex attempted an unexpected tool operation; proposal rejected.", raw)
            if process.returncode != 0 or failures or len(completed) != 1:
                raise ProviderError("Codex did not complete the proposal. Inspect response.json for model-access or connection errors.", raw)
            proposal_path = directory / "proposal.json"
            if not proposal_path.is_file() or proposal_path.stat().st_size > MAX_RESPONSE_BYTES:
                raise ProviderError("Codex did not write a valid-sized final response file.", raw)
            text = proposal_path.read_text()
            if not text.strip():
                raise ProviderError("Codex returned an empty final response.", raw)
            # The common task compiler validates JSON and simulation feasibility next.
            return {"text": text, "raw": raw, "usage": completed[0].get("usage", {}),
                    "http_attempts": 0, "codex_invocations": 1}
