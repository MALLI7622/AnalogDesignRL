import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from analog_design.codex_client import CodexClient, CodexConfig, codex_environment
from analog_design.model_clients import ProviderError
from analog_design.simulator import ROOT


SCHEMA = {"type": "object", "properties": {"value": {"type": "number"}},
          "required": ["value"], "additionalProperties": False}


def client_ready():
    client = CodexClient(CodexConfig(provider="codex", model="gpt-6-astra"))
    client.executable = "/test/codex"
    client.version = "codex-cli test"
    return client


def process_factory(*, output='{"value":1}', events=None, exit_code=0):
    if events is None:
        events = [{"type": "thread.started", "thread_id": "test"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": output}},
                  {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}]

    def start(command, **kwargs):
        directory = Path(command[command.index("--cd") + 1])
        process = MagicMock(returncode=exit_code, pid=12345)

        def communicate(prompt=None, timeout=None):
            if output is not None:
                (directory / "proposal.json").write_text(output)
            for event in events:
                kwargs["stdout"].write(json.dumps(event) + "\n")
            return None, None

        process.communicate.side_effect = communicate
        return process
    return start


class CodexClientTests(unittest.TestCase):
    def test_saved_chatgpt_login_is_used_without_api_environment(self):
        outputs = [subprocess.CompletedProcess([], 0, "codex-cli 0.153.4\n", ""),
                   subprocess.CompletedProcess([], 0, "--output-schema --output-last-message --ephemeral --ignore-user-config --json", ""),
                   subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT")]
        client = CodexClient(CodexConfig(provider="codex", model="gpt-6-astra"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "api-secret", "CODEX_API_KEY": "api-secret", "CODEX_HOME": "/saved/login"}), \
             patch("analog_design.codex_client.shutil.which", return_value="/test/codex"), \
             patch("analog_design.codex_client.subprocess.run", side_effect=outputs) as run:
            client.check_credentials()
            self.assertEqual(client.version, "codex-cli 0.153.4")
            for call in run.call_args_list:
                env = call.kwargs["env"]
                self.assertNotIn("OPENAI_API_KEY", env)
                self.assertNotIn("CODEX_API_KEY", env)
                self.assertEqual(env["CODEX_HOME"], "/saved/login")

    def test_missing_cli_old_cli_and_api_login_fail_clearly(self):
        client = CodexClient(CodexConfig(provider="codex", model="gpt-6-astra"))
        with patch("analog_design.codex_client.shutil.which", return_value=None), self.assertRaisesRegex(ProviderError, "missing"):
            client.check_credentials()
        for help_text, login_text, expected in (("", "", "Update Codex"),
                    ("--output-schema --output-last-message --ephemeral --ignore-user-config --json", "Logged in using an API key", "codex login")):
            outputs = [subprocess.CompletedProcess([], 0, "codex-cli test", ""),
                       subprocess.CompletedProcess([], 0, help_text, ""),
                       subprocess.CompletedProcess([], 0, login_text, "")]
            with patch("analog_design.codex_client.shutil.which", return_value="/test/codex"), \
                 patch("analog_design.codex_client.subprocess.run", side_effect=outputs), self.assertRaisesRegex(ProviderError, expected):
                client.check_credentials()

    def test_preview_has_no_login_or_cli_invocation(self):
        client = CodexClient(CodexConfig(provider="codex", model="gpt-6-astra"))
        with patch("analog_design.codex_client.subprocess.run") as run:
            request = client.prepare("instructions", [{"role": "user", "content": "source"}], SCHEMA)
            run.assert_not_called()
        command = request["command"]
        self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", command)
        self.assertIn("shell_tool", command)
        self.assertIn("--ephemeral", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_complete_uses_stdin_isolated_directory_and_final_file(self):
        client = client_ready()
        request = client.prepare("instructions", [{"role": "user", "content": "source material"}], SCHEMA)
        with patch("analog_design.codex_client.subprocess.Popen", side_effect=process_factory()) as popen:
            reply = client.complete(request)
            call = popen.call_args
            self.assertTrue(call.kwargs["start_new_session"])
            self.assertNotIn("shell", call.kwargs)
            command = call.args[0]
            self.assertEqual(command[-1], "-")
            self.assertNotIn("source material", " ".join(command))
            directory = Path(command[command.index("--cd") + 1])
            self.assertNotEqual(directory, ROOT)
            self.assertFalse(directory.exists())
        self.assertEqual(json.loads(reply["text"]), {"value": 1})
        self.assertEqual(reply["usage"]["input_tokens"], 100)
        self.assertEqual(reply["codex_invocations"], 1)

    def test_failed_or_incomplete_turn_cannot_pass_even_with_a_final_file(self):
        cases = [({"events": [], "exit_code": 1}),
                 {"events": [{"type": "turn.failed", "error": {"message": "unavailable model"}}]},
                 {"events": [{"type": "thread.started"}]},
                 {"output": None}, {"output": ""}]
        for options in cases:
            with self.subTest(options=options), patch("analog_design.codex_client.subprocess.Popen", side_effect=process_factory(**options)):
                client = client_ready()
                with self.assertRaises(ProviderError):
                    client.complete(client.prepare("s", [], SCHEMA))

    def test_tool_operations_are_rejected(self):
        for kind in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
            events = [{"type": "item.completed", "item": {"type": kind}}, {"type": "turn.completed"}]
            with self.subTest(kind=kind), patch("analog_design.codex_client.subprocess.Popen", side_effect=process_factory(events=events)):
                client = client_ready()
                with self.assertRaisesRegex(ProviderError, "unexpected tool"):
                    client.complete(client.prepare("s", [], SCHEMA))

    def test_nonfatal_cli_diagnostic_does_not_reject_a_completed_turn(self):
        events = [{"type": "item.completed", "item": {"type": "error", "message":
                   "Code Mode is unavailable because code-mode host is disabled."}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": '{"value":1}'}},
                  {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}]
        with patch("analog_design.codex_client.subprocess.Popen", side_effect=process_factory(events=events)):
            client = client_ready()
            reply = client.complete(client.prepare("s", [], SCHEMA))
        self.assertEqual(json.loads(reply["text"]), {"value": 1})
        self.assertIn("Code Mode is unavailable", reply["raw"]["events_jsonl"])

    def test_timeout_terminates_worker_process_group(self):
        process = MagicMock(pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 300), (None, None)]
        client = client_ready()
        with patch("analog_design.codex_client.subprocess.Popen", return_value=process), \
             patch("analog_design.codex_client.os.killpg") as kill, self.assertRaisesRegex(ProviderError, "exceeded"):
            client.complete(client.prepare("s", [], SCHEMA))
        kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)

    def test_configuration_rejects_invalid_timeouts_and_provider(self):
        for options in ({"provider": "openai"}, {"timeout_s": float("nan")}, {"timeout_s": True}, {"timeout_s": -1}):
            with self.assertRaises(ValueError):
                CodexConfig(**{"provider": "codex", "model": "gpt-6-astra", **options})

    def test_api_key_variables_are_filtered_without_mutating_parent_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret", "OPENAI_BASE_URL": "https://example.invalid"}):
            env = codex_environment()
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_API_KEY", env)
            self.assertNotIn("OPENAI_BASE_URL", env)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "secret")

    def test_codex_cli_config_produces_a_dry_run_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(["python3", str(ROOT / "scripts/generate_tasks.py"),
                                        "--config", str(ROOT / "generation/providers/codex.json"),
                                        "--dry-run", "--output", str(Path(directory) / "preview")],
                                       capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "dry_run")
            request = json.loads((Path(directory) / "preview/request_preview.json").read_text())
            self.assertEqual(request["transport"], "codex_exec")


if __name__ == "__main__":
    unittest.main()
