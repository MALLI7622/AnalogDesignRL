"""Generate circuit-sizing tasks with an AI provider and verify each in ngspice."""
import argparse
from datetime import datetime
from pathlib import Path
import json
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analog_design.model_clients import ModelClient, ModelConfig, PRESETS, ProviderError, ReplayClient, strict_json
from analog_design.codex_client import CodexClient, CodexConfig
from analog_design.task_generation import generate_tasks
from analog_design.task_proposals import TEMPLATES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Provider configuration JSON (API keys belong in environment variables)")
    parser.add_argument("--provider", choices=(*PRESETS, "codex"))
    parser.add_argument("--model", help="Exact model ID accepted by the selected endpoint")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env", help="Environment variable name, never the key itself")
    parser.add_argument("--json-mode", choices=("schema", "object", "prompt"))
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--request-timeout", type=float)
    parser.add_argument("--retries", type=int)
    parser.add_argument("--codex-bin", help="Codex executable path (only with --provider codex)")
    parser.add_argument("--template", choices=TEMPLATES, default="autockt_two_stage")
    parser.add_argument("--source", type=Path, action="append", default=[], help="Local paper PDF or text; repeat for multiple sources")
    parser.add_argument("--exclude-batch", type=Path, action="append", default=[], help="Avoid accepted tasks from an earlier batch directory; repeat as needed")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, help="Total model proposals, including revisions (default: 3 × count)")
    parser.add_argument("--simulator-timeout", type=float, default=30)
    parser.add_argument("--max-source-chars", type=int, default=100000)
    parser.add_argument("--output", type=Path, help="New dataset directory; existing paths are never overwritten")
    parser.add_argument("--dry-run", action="store_true", help="Write the request without calling a model or simulator")
    parser.add_argument("--replay", type=Path, help="Offline recorded proposal(s), marked as replay in provenance")
    args = parser.parse_args()
    try:
        if args.replay:
            if args.config or any(getattr(args, key) is not None for key in
                                  ("provider", "model", "base_url", "api_key_env", "json_mode", "max_output_tokens", "request_timeout", "retries", "codex_bin")):
                parser.error("--replay cannot be combined with provider options")
            client = ReplayClient(args.replay)
        else:
            config = strict_json(args.config.read_text()) if args.config else {}
            if not isinstance(config, dict):
                raise ValueError("Provider configuration must be a JSON object.")
            for key in ("provider", "model", "base_url", "api_key_env", "json_mode", "max_output_tokens", "retries"):
                if getattr(args, key) is not None:
                    config[key] = getattr(args, key)
            if args.request_timeout is not None:
                config["timeout_s"] = args.request_timeout
            if args.codex_bin is not None:
                config["executable"] = args.codex_bin
            if "provider" not in config or "model" not in config:
                parser.error("supply --provider and --model, or a config containing both")
            client = CodexClient(CodexConfig(**config)) if config["provider"] == "codex" else ModelClient(ModelConfig(**config))
        output = args.output or ROOT / "datasets" / ("generated_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
        manifest = generate_tasks(client, args.template, output, source_paths=args.source, count=args.count,
                                  max_attempts=args.max_attempts, simulator_timeout=args.simulator_timeout,
                                  max_source_chars=args.max_source_chars, dry_run=args.dry_run,
                                  exclude_batches=args.exclude_batch,
                                  progress=lambda item: print(json.dumps(item), flush=True))
        print(json.dumps({"directory": str(output.resolve()), "status": manifest["status"],
                          "accepted": len(manifest["accepted"]), "requested": manifest["requested"],
                          "attempts": manifest["attempts_used"], "generation_mode": manifest["generation_mode"],
                          **({"error": manifest["error"]} if "error" in manifest else {})}, indent=2))
        return 0 if manifest["status"] in {"complete", "dry_run"} else 2 if manifest["status"] == "provider_error" else 1
    except (ValueError, TypeError, OSError, ProviderError) as exc:
        parser.exit(2, f"Generation error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
