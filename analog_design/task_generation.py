"""Generate bounded AI proposals, verify them, and write an auditable task dataset."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil

from .generation_sources import load_sources
from .model_clients import ProviderError, strict_json
from .simulator import ROOT, digest, evaluate
from .task_proposals import TEMPLATES, compile_proposal, load_template, parse_proposal, proposal_schema, validate_schema


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def summarize_result(result):
    return {key: result[key] for key in ("status", "success", "metrics", "checks", "error") if key in result}


def preflight():
    if not shutil.which("ngspice") or not (ROOT / ".deps/sky130_pdk/libs.tech/ngspice/corners/tt.spice").is_file():
        raise ValueError("Install ngspice and run python3 scripts/setup.py before generating tasks.")


def load_exclusions(directories, template_name, schema):
    """Snapshot accepted tasks from earlier batches, including partially finished ones."""
    records, entries = [], {}
    for directory in directories:
        directory = Path(directory).resolve()
        manifest_path = directory / "manifest.json"
        prior = strict_json(manifest_path.read_text())
        if not isinstance(prior, dict) or prior.get("template") != template_name or not isinstance(prior.get("accepted"), list):
            raise ValueError("Excluded batches must have an accepted-task list for the same circuit template.")
        for entry in prior["accepted"]:
            if not isinstance(entry, dict) or not all(key in entry for key in ("path", "sha256", "id", "fingerprint", "initial_parameters", "constraints")):
                raise ValueError("Excluded batch contains an invalid task record.")
            path = (directory / entry["path"]).resolve()
            if not path.is_relative_to(directory) or digest(path) != entry["sha256"]:
                raise ValueError("Excluded task path or hash does not match its batch record.")
            task = strict_json(path.read_text())
            fingerprint = entry["fingerprint"]
            if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
                raise ValueError("Excluded task has an invalid fingerprint.")
            if not isinstance(task, dict) or task.get("id") != template_name + "_ai_" + fingerprint[:20]:
                raise ValueError("Excluded task identity does not match its batch record.")
            for key in ("initial_parameters", "constraints"):
                validate_schema(entry[key], schema["properties"][key], "excluded_task." + key)
                if task.get(key) != entry[key]:
                    raise ValueError("Excluded task contents do not match its batch record.")
            entries[fingerprint] = {key: entry[key] for key in ("id", "fingerprint", "initial_parameters", "constraints")}
        records.append({"directory": str(directory), "manifest_sha256": digest(manifest_path)})
    if len(entries) > 1000:
        raise ValueError("At most 1000 prior tasks may be excluded in one batch.")
    return records, list(entries.values())


def generate_tasks(client, template_name, output, *, source_paths=(), count=1, max_attempts=None,
                   simulator_timeout=30, max_source_chars=100000, dry_run=False, progress=None,
                   exclude_batches=()):
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("count must be an integer between 1 and 1000.")
    max_attempts = count * 3 if max_attempts is None else max_attempts
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10000:
        raise ValueError("max_attempts must be an integer between 1 and 10000.")
    if isinstance(simulator_timeout, bool) or not isinstance(simulator_timeout, (int, float)) or not math.isfinite(simulator_timeout) or not 0 < simulator_timeout <= 300:
        raise ValueError("simulator_timeout must be positive, finite, and at most 300 seconds.")
    if type(max_source_chars) is not int or not 1000 <= max_source_chars <= 2000000:
        raise ValueError("max_source_chars must be between 1000 and 2000000.")
    template, seed_reference = load_template(template_name)
    sources = load_sources(template, source_paths, max_source_chars)
    schema = proposal_schema(template, sources)
    excluded_batches, excluded_tasks = load_exclusions(exclude_batches, template_name, schema)
    system = (ROOT / "generation/prompt.txt").read_text()
    if not dry_run:
        preflight()
        client.check_credentials()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "tasks").mkdir()
    (output / "private").mkdir()
    (output / "sources").mkdir()
    for source_id, source in sources.items():
        (output / "sources" / (source_id + ".txt")).write_text(source["text"])
    (output / "system_prompt.txt").write_text(system)
    save_json(output / "proposal_schema.json", schema)
    files = ["analog_design/model_clients.py", "analog_design/codex_client.py", "analog_design/generation_sources.py",
             "analog_design/task_generation.py", "analog_design/task_proposals.py", "generation/prompt.txt",
             "scripts/generate_tasks.py",
             "analog_design/simulator.py", "analog_design/metrics.py", "analog_design/crosscheck.py",
             "dependencies.lock.json", TEMPLATES[template_name],
             TEMPLATES[template_name].replace("_sizing", "_nominal")]
    files.extend(template["circuit_directory"] + "/" + name for name in
                 ("netlist.spice", "reference.params", "source.json"))
    fingerprints = {name: digest(ROOT / name) for name in files}
    manifest = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                "status": "running", "generation_mode": client.mode, "generator": client.describe(),
                "template": template_name, "code_and_inputs_sha256": fingerprints,
                "sources": {key: {k: v for k, v in source.items() if k != "text"} for key, source in sources.items()},
                "requested": count, "max_attempts": max_attempts, "attempts_used": 0,
                "excluded_batches": excluded_batches, "excluded_tasks": excluded_tasks,
                "simulator_timeout_s": simulator_timeout, "accepted": [], "rejected": [],
                "training_approved": False, "production_ready": False,
                "scope": "Sizing proposals for one fixed circuit and nominal testbench; no automatic paper-result reproduction.",
                "split_status": "unassigned; group by requirement_group before training/evaluation"}
    save_json(output / "manifest.json", manifest)
    base_context = {"template": template, "seed_reference_parameters": seed_reference,
                    "sources": sources, "output_schema": schema}
    base_message = json.dumps(base_context, indent=2, allow_nan=False)
    last_text = None
    last_feedback = None
    seen = {entry["fingerprint"] for entry in excluded_tasks}
    try:
        for attempt in range(1, max_attempts + 1):
            messages = [{"role": "user", "content": base_message}]
            messages[0]["content"] += "\n" + json.dumps({
                "instruction": "Generate a new task. Avoid repeating these accepted starting values and requirements, including those from earlier batches.",
                "accepted_tasks": [{"initial_parameters": entry["initial_parameters"], "constraints": entry["constraints"]}
                                   for entry in [*excluded_tasks, *manifest["accepted"]]]})
            if last_text is not None:
                messages.append({"role": "assistant", "content": last_text})
                messages.append({"role": "user", "content": json.dumps({
                    "validation_feedback": last_feedback,
                    "instruction": "Revise the rejected proposal and return the complete JSON object."})})
            request = client.prepare(system, messages, schema)
            if dry_run:
                save_json(output / "request_preview.json", request)
                manifest["status"] = "dry_run"
                break
            attempt_dir = output / "attempts" / f"{attempt:05d}"
            attempt_dir.mkdir(parents=True)
            save_json(attempt_dir / "request.json", request)
            manifest["attempts_used"] = attempt
            save_json(output / "manifest.json", manifest)
            if progress:
                progress({"attempt": attempt, "status": "generating", "accepted": len(manifest["accepted"]), "requested": count})
            try:
                reply = client.complete(request)
            except ProviderError as exc:
                if exc.response is not None:
                    save_json(attempt_dir / "response.json", exc.response)
                manifest["status"] = "provider_error"
                manifest["error"] = str(exc)
                save_json(attempt_dir / "outcome.json", {"status": "provider_error", "error": str(exc)})
                break
            save_json(attempt_dir / "response.json", reply)
            last_text = reply["text"]
            feedback = {}
            try:
                proposal = parse_proposal(last_text)
                save_json(attempt_dir / "proposal.json", proposal)
                task, reference, fingerprint, group = compile_proposal(proposal, template, template_name, sources)
                if fingerprint in seen:
                    raise ValueError("Duplicate task: these starting parameters and requirements were already accepted.")
                task_path = attempt_dir / "task.json"
                save_json(task_path, task)
                reference_result = evaluate(task_path, reference, attempt_dir / "reference", simulator_timeout)
                feedback["reference"] = summarize_result(reference_result)
                if reference_result["status"] != "ok" or reference_result["success"] is not True:
                    raise ValueError("Reference did not pass all simulation checks. This proposal is unverified, not proven infeasible.")
                initial_result = evaluate(task_path, {}, attempt_dir / "initial", simulator_timeout)
                feedback["initial"] = summarize_result(initial_result)
                if initial_result["status"] != "ok":
                    raise ValueError("Starting design must produce valid measurements; a simulator/verifier error is not an accepted failing start.")
                if initial_result["success"] is not False:
                    raise ValueError("Starting design already passes; propose a task requiring improvement.")
                if any(digest(ROOT / name) != expected for name, expected in fingerprints.items()):
                    manifest["status"] = "inputs_changed"
                    manifest["error"] = "Source, template, or evaluator files changed during generation. Start a new run."
                    save_json(attempt_dir / "outcome.json", {"status": "rejected", "error": manifest["error"]})
                    break
                save_json(output / "tasks" / (task["id"] + ".json"), task)
                private = output / "private" / task["id"]
                save_json(private / "reference.json", reference)
                save_json(private / "provenance.json", {
                    "generation_mode": client.mode, "generator": client.describe(), "proposal": proposal,
                    "numeric_value_origin": "Model proposal (or replay fixture), checked under the recorded simulation conditions.",
                    "paper_result_reproduced": False, "source_interpretation_reviewed": False,
                    "attempt": str(attempt_dir.relative_to(output)),
                    "task_sha256": digest(output / "tasks" / (task["id"] + ".json")),
                    "reference_metrics": reference_result["metrics"],
                    "initial_metrics": initial_result["metrics"], "requirement_group": group,
                    "simulation_verified": True, "training_approved": False})
                entry = {"id": task["id"], "path": "tasks/" + task["id"] + ".json",
                         "sha256": digest(output / "tasks" / (task["id"] + ".json")),
                         "fingerprint": fingerprint, "requirement_group": group, "attempt": attempt,
                         "initial_parameters": task["initial_parameters"], "constraints": task["constraints"]}
                manifest["accepted"].append(entry)
                seen.add(fingerprint)
                outcome = {"status": "accepted", "task_id": task["id"], **feedback}
                last_text, last_feedback = None, None
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                # Nothing in model output is executable; malformed proposals only consume an attempt.
                outcome = {"status": "rejected", "error": str(exc), **feedback}
                manifest["rejected"].append({"attempt": attempt, "reason": str(exc)})
                last_feedback = outcome
                # Retain bounded feedback; oversized replies remain in the raw artifact.
                if len(last_text) > 30000:
                    last_text = "Previous proposal exceeded 30000 characters and was rejected."
            save_json(attempt_dir / "outcome.json", outcome)
            save_json(output / "manifest.json", manifest)
            if progress:
                progress({"attempt": attempt, "status": outcome["status"], "accepted": len(manifest["accepted"]),
                          "requested": count, **({"reason": outcome["error"]} if "error" in outcome else {})})
            if len(manifest["accepted"]) == count:
                manifest["status"] = "complete"
                break
        if manifest["status"] == "running":
            manifest["status"] = "attempt_budget_exhausted"
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        raise
    except Exception:
        manifest["status"] = "internal_error"
        raise
    finally:
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(output / "manifest.json", manifest)
    return manifest
