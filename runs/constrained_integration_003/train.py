"""Gemma 3 1B LoRA GRPO on a TPU, with ngspice behind the worker API."""
import argparse
from dataclasses import replace
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import threading
import time

from training.client import RemoteEpisode, SYSTEM_PROMPT, WorkerClient, WorkerError, action_prompt, system_prompt
from training.chat_format import GemmaChatParser, PromptWindowExceeded, verify_chat_contract, completion_alignment

TRAINING_MODES = {"train", "research-pilot"}


def sequence_microbatches(examples, group_size, microbatch_size):
    """Split already-computed GRPO advantages without regrouping rewards."""
    import jax
    import numpy as np
    for example in examples:
        count = example.completion_ids.shape[0]
        if count != group_size or count % microbatch_size:
            raise ValueError("Expected one complete GRPO group for sequence accumulation")
        # Tunix's sequence mean excludes empty masks. Uniform accumulation would
        # change that denominator if any sequence were completely filtered out.
        if np.any(np.asarray(example.completion_mask).sum(axis=-1) == 0):
            raise ValueError("Cannot uniformly accumulate a GRPO group with empty response masks")
        for start in range(0, count, microbatch_size):
            yield jax.tree_util.tree_map(
                lambda x: x[start:start + microbatch_size]
                if hasattr(x, "shape") and x.shape and x.shape[0] == count else x,
                example)


def install_sequence_accumulation(cluster, group_size, microbatch_size):
    original = cluster.update_actor

    def update_actor(train_ds, eval_ds, skip_jit=False):
        chunks = list(sequence_microbatches(train_ds, group_size, microbatch_size))
        eval_chunks = (list(sequence_microbatches(eval_ds, group_size, microbatch_size))
                       if eval_ds else None)
        return original(chunks, eval_chunks, skip_jit)

    cluster.update_actor = update_actor


class SeededGeneration:
    """Assign a fresh integer seed to each sampler call, including GRPO calls.

    Reproducible for the same call order and batching. Async scheduling can change
    that order, so record prompt hashes and seeds for auditing. Never mutate the
    shared RolloutConfig or retain a JAX key that the sampler can donate.
    """

    def __init__(self, generate, seed, log_path, generations_path=None, alignment_check=None,
                 require_alignment=False):
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise ValueError("seed must be an unsigned 32-bit integer")
        self.generate, self.seed = generate, seed
        self.alignment_check, self.require_alignment = alignment_check, require_alignment
        self.log_path = Path(log_path)
        with self.log_path.open("x"):
            pass
        self.calls = 0
        self.episode_scope = None
        self.episode_calls = 0
        self.lock = threading.Lock()
        self.generations_path = Path(generations_path) if generations_path else None
        if self.generations_path is not None:
            with self.generations_path.open("x"):
                pass

    def set_episode(self, task_id, episode_index):
        """Pair rollout seeds across arms even when earlier episodes end early."""
        if not isinstance(task_id, str) or not task_id or type(episode_index) is not int or episode_index < 1:
            raise ValueError("Expected a task ID and positive episode index")
        with self.lock:
            self.episode_scope = (task_id, episode_index)
            self.episode_calls = 0

    def __call__(self, prompts, rollout_config, **kwargs):
        # Serialize access to the shared sampler and its seed ledger.
        with self.lock:
            prompt_list = [prompts] if isinstance(prompts, str) else list(prompts)
            if not prompt_list or any(not isinstance(p, str) for p in prompt_list):
                raise ValueError("Generation requires a string prompt or a nonempty sequence of strings")
            if self.calls >= 2**32:
                raise RuntimeError("Generation seed sequence exhausted")
            call = self.calls
            # Odd stride is a permutation modulo 2**32: no seed repeats.
            seed = (self.seed + call * 0x9E3779B9) % 2**32
            self.calls += 1
            record = {"call": call, "seed": seed, "prompt_count": len(prompt_list),
                      "prompt_sha256": [hashlib.sha256(p.encode()).hexdigest() for p in prompt_list]}
            if self.episode_scope is not None:
                task_id, episode_index = self.episode_scope
                material = json.dumps(["paired_episode_v1", self.seed, task_id, episode_index,
                                       self.episode_calls], separators=(",", ":")).encode()
                seed = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
                record.update(seed=seed, task_id=task_id, episode=episode_index,
                              episode_call=self.episode_calls)
                self.episode_calls += 1
            # Record before invoking: failed calls consume their seed too.
            with self.log_path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            if self.generations_path is not None:
                with self.generations_path.open("a") as stream:
                    stream.write(json.dumps({**record, "event": "request", "prompts": prompt_list}) + "\n")
            try:
                result = self.generate(prompts, replace(rollout_config, seed=seed), **kwargs)
            except Exception as exc:
                if self.generations_path is not None:
                    with self.generations_path.open("a") as stream:
                        stream.write(json.dumps({**record, "event": "error", "error_type": type(exc).__name__}) + "\n")
                raise
            alignment = self.alignment_check(prompt_list, result) if self.alignment_check else None
            if self.generations_path is not None:
                token_data = getattr(result, 'tokens', None)
                completion_metadata = ({'completion_token_counts': [len(tokens) for tokens in token_data],
                    'completion_terminal_token_ids': [int(tokens[-1]) if len(tokens) else None for tokens in token_data],
                    'completion_tokens': [[int(t) for t in tokens] for tokens in token_data]}
                    if token_data is not None else {})
                if getattr(result, 'logprobs', None) is not None:
                    completion_metadata['completion_logprobs'] = [[float(p) for p in row] for row in result.logprobs]
                with self.generations_path.open("a") as stream:
                    stream.write(json.dumps({**record, "event": "response", "responses": result.text,
                                             "training_token_alignment": alignment, **completion_metadata}) + "\n")
            if self.require_alignment and (alignment is None or not alignment['valid']):
                raise RuntimeError('Sampled tokens do not match canonical training context; stopping before optimizer use')
            return result


def rollout_requests(tasks, episodes_per_task, skip_episodes=0):
    index = 0
    for task in tasks:
        for episode in range(1, episodes_per_task + 1):
            if index >= skip_episodes:
                yield task, episode
            index += 1


def select_tasks(catalog, split, limit=None):
    """Select a reproducible subset without rewriting the catalog's splits."""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("Task limits must be positive integers")
    rows = sorted((row for row in catalog["tasks"] if row["split"] == split), key=lambda row: row["id"])
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate task IDs in the requested split")
    if not rows:
        raise ValueError("No tasks in the requested split: " + split)
    return rows if limit is None else rows[:limit]


def execution_plan(config, validation_count=None):
    # Pinned Tunix evaluates at step zero, then at multiples of eval_every.
    rounds = (config["max_updates"] - 1) // config["eval_every"] + 1
    bound = validation_count if validation_count is not None else config.get("validation_task_limit")
    training = config["max_updates"] * config["num_generations"] * config["max_episode_steps"]
    validation = None if bound is None else rounds * bound * config["num_generations"] * config["max_episode_steps"]
    return {"model": config["model_id"], "algorithm": "LoRA + multi-turn GRPO",
            "updates": config["max_updates"], "episodes_per_update": config["num_generations"],
            "max_training_episodes": config["max_updates"] * config["num_generations"],
            "max_validation_episodes": None if bound is None else rounds * bound * config["num_generations"],
            "max_total_episodes": None if bound is None else (config["max_updates"] + rounds * bound) * config["num_generations"],
            "max_evaluations_per_episode": config["max_episode_steps"],
            "max_training_evaluations": training, "max_training_simulator_invocations": 2 * training,
            "validation_rounds": rounds, "validation_task_limit": config.get("validation_task_limit"),
            "selected_validation_task_count": validation_count,
            "max_validation_evaluations": validation,
            "max_total_evaluations": None if validation is None else training + validation,
            "note": "Validation includes step zero. A configured validation limit is an upper bound until the catalog is loaded. Baseline/rollout runs are separate. No weights downloaded or compute created by plan mode."}


def argument_parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", default="training/configs/gemma3_1b.json")
    cli.add_argument("--mode", choices=["plan", "rollout", "train", "research-pilot"], default="plan")
    cli.add_argument("--output", default=None, help="New local run directory")
    cli.add_argument("--checkpoint-uri", help="Optional dedicated gs://... checkpoint prefix")
    cli.add_argument("--split", choices=["smoke", "train", "validation", "test"], default="smoke", help="Rollout mode only")
    cli.add_argument("--max-tasks", type=int, help="Rollout only: use the first N sorted task IDs in the split")
    cli.add_argument("--episodes-per-task", type=int, default=1,
                     help="Rollout only: independent episodes from each task's original start")
    cli.add_argument("--skip-rollout-episodes", type=int, default=0,
                     help="Rollout only: skip this many task/episode pairs; requires paired episode seeds")
    cli.add_argument("--restore-checkpoint", help="Rollout only: existing training checkpoint root (containing actor/)")
    cli.add_argument("--restore-run", type=Path, help="Rollout only: source run.json binding the checkpoint and base model")
    cli.add_argument("--pilot-manifest", type=Path, help="Authorized, bounded research-pilot manifest; required only in that mode")
    return cli


def validate_run_arguments(args):
    if args.skip_rollout_episodes < 0 or (args.skip_rollout_episodes and args.mode != "rollout"):
        raise ValueError("--skip-rollout-episodes requires rollout mode and a nonnegative count")
    if args.checkpoint_uri and not args.checkpoint_uri.startswith("gs://"):
        raise ValueError("--checkpoint-uri must be a gs:// bucket prefix")
    if args.max_tasks is not None and (args.mode != "rollout" or args.max_tasks < 1):
        raise ValueError("--max-tasks requires rollout mode and a positive integer")
    if args.episodes_per_task < 1 or (args.mode != "rollout" and args.episodes_per_task != 1):
        raise ValueError("--episodes-per-task requires rollout mode and a positive integer")
    if bool(args.restore_checkpoint) != bool(args.restore_run):
        raise ValueError("Use --restore-checkpoint and --restore-run together")
    if args.restore_checkpoint and args.mode != "rollout":
        raise ValueError("Checkpoint loading is supported only for rollout; training resume is not implemented")
    if (args.mode == "research-pilot") != bool(args.pilot_manifest):
        raise ValueError("--pilot-manifest is required only for --mode research-pilot")


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _checkpoint_root(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A nonempty checkpoint root is required")
    return value.rstrip("/") if value.startswith("gs://") else str(Path(value).resolve())


def restore_source(checkpoint_root, run_path, config):
    """Read local provenance before importing JAX or downloading any weights."""
    run_path = Path(run_path).resolve()
    source = json.loads(run_path.read_text())
    original = source["config"]
    revision = original.get("model_revision", "")
    if source.get("mode") not in TRAINING_MODES:
        raise ValueError("Restore source must be a training or explicitly bounded research-pilot run")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Source run must record an immutable Hugging Face revision")
    if original["model_id"] != config["model_id"] or original["lora"] != config["lora"]:
        raise ValueError("Restore model ID or LoRA configuration differs from the source run")
    if config["model_revision"] not in {"main", revision}:
        raise ValueError("Configured model revision differs from the checkpoint's recorded base")
    root = _checkpoint_root(checkpoint_root)
    if root != _checkpoint_root(source.get("checkpoint_uri")):
        raise ValueError("Checkpoint root does not match the source run; relocation requires separately verified provenance")
    record = {"checkpoint_root": root, "source_run": str(run_path), "source_run_sha256": _sha256(run_path),
              "source_mode": source["mode"], "model_id": original["model_id"], "model_revision": revision,
              "lora_config_sha256": _canonical_sha256(original["lora"]), "source_result": None}
    result_path = run_path.with_name("training_result.json")
    if result_path.exists():
        result = json.loads(result_path.read_text())
        if _checkpoint_root(result.get("checkpoint_root")) != root:
            raise ValueError("Source result checkpoint root disagrees with run.json")
        for field in ("checkpoint_step", "optimizer_steps"):
            if type(result.get(field)) is not int or result[field] < 1:
                raise ValueError("Source training result has no positive " + field)
        if not re.fullmatch(r"[0-9a-f]{64}", result.get("final_adapter_sha256", "")):
            raise ValueError("Source training result has no valid final adapter hash")
        record.update(source_result=result, source_result_path=str(result_path), source_result_sha256=_sha256(result_path))
    return record


def validate_restored_checkpoint(source, step, metadata, adapter_sha256):
    if type(step) is not int or step < 1:
        raise RuntimeError("No positive training checkpoint was restored")
    if not isinstance(metadata, dict) or type(metadata.get("global_step")) is not int or metadata["global_step"] < 1:
        raise RuntimeError("Checkpoint is missing positive training-step metadata")
    expected = {key: source[key] for key in ("source_run_sha256", "model_id", "model_revision", "lora_config_sha256")}
    expected.update(role="actor", checkpoint_step=step)
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Checkpoint metadata is not bound to the source run and base model")
    result = source.get("source_result")
    if result is not None:
        if step != result["checkpoint_step"] or metadata["global_step"] != result["optimizer_steps"]:
            raise RuntimeError("Latest checkpoint does not match the source run's completed final step")
        if adapter_sha256 != result["final_adapter_sha256"]:
            raise RuntimeError("Restored adapter hash differs from the source training result")
    return {"checkpoint_step": step, "global_step": metadata["global_step"], "adapter_sha256": adapter_sha256,
            "final_adapter_hash_verified": result is not None}


def restore_adapter(model, source, adapter_digest, manager_factory):
    """Restore through Tunix's API; never treat its zero-step fallback as success."""
    if _sha256(source["source_run"]) != source["source_run_sha256"]:
        raise RuntimeError("Source run changed before checkpoint loading")
    if source.get("source_result_path") and _sha256(source["source_result_path"]) != source["source_result_sha256"]:
        raise RuntimeError("Source training result changed before checkpoint loading")
    manager = manager_factory(source["checkpoint_root"] + "/actor")
    try:
        latest = manager.latest_step()
        if type(latest) is not int or latest < 1:
            raise RuntimeError("Checkpoint root contains no positive saved step")
        step, metadata = manager.maybe_restore(model, step=latest, restore_only_lora_params=True)
        if step != latest:
            raise RuntimeError("Checkpoint restore did not return the requested latest step")
        return validate_restored_checkpoint(source, step, metadata, adapter_digest(model))
    finally:
        manager.close()


@contextmanager
def closing_cluster(cluster, before_close=None):
    """Close once even when the pinned learner closes its cluster internally."""
    original_close = cluster.close
    closed = False

    def close_once():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            if before_close is not None:
                before_close(cluster)
        finally:
            original_close()

    cluster.close = close_once
    try:
        yield cluster
    finally:
        cluster.close()


def save_final_checkpoint(cluster):
    """Tunix's fallback final save omits metadata; explicitly retain our binding."""
    trainer = cluster.actor_trainer
    step = trainer.train_steps
    if step > 0 and (trainer.checkpoint_manager.latest_step() or 0) < step:
        metadata = {**trainer.custom_checkpoint_metadata(), "global_step": max(1, int(cluster.global_steps))}
        if not trainer.checkpoint_manager.save(step, trainer.model, trainer.optimizer,
                save_only_lora_params=True, force=True, custom_metadata=metadata):
            raise RuntimeError("The final adapter checkpoint was not saved")


def batches(tasks, count, seed):
    rng = random.Random(seed)
    for _ in range(count):
        task = rng.choice(tasks)
        # Tunix constructs one environment per group member using this task ID.
        yield {"prompts": ["Solve the circuit sizing task using the simulator."], "task_id": [task["id"]]}


def validate_config(config):
    if config.get('constrained_compute_dtype', 'bfloat16') not in {'bfloat16', 'float32'}:
        raise ValueError('constrained_compute_dtype must be bfloat16 or float32')
    if 'verify_constrained_probabilities' in config and type(config['verify_constrained_probabilities']) is not bool:
        raise ValueError('verify_constrained_probabilities must be boolean')
    if 'constrained_parameter_keys' in config and type(config['constrained_parameter_keys']) is not bool:
        raise ValueError('constrained_parameter_keys must be boolean')
    system_prompt(config.get("prompt_variant", "current"))
    if config.get("rollout_seed_policy", "sequential") not in {"sequential", "paired_episode_v1"}:
        raise ValueError("Unknown rollout_seed_policy")
    if type(config.get("seed")) is not int or not 0 <= config["seed"] < 2**32:
        raise ValueError("seed must be an unsigned 32-bit integer")
    if config["model_id"] != "google/gemma-3-1b-it":
        raise ValueError("This loader implements Gemma 3 1B-IT; another architecture needs its own loader")
    for key in ("max_episode_steps", "max_prompt_tokens", "max_response_tokens", "max_updates",
                "num_generations", "max_concurrency", "checkpoint_every", "eval_every"):
        if isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if config["num_generations"] < 2:
        raise ValueError("GRPO needs at least two samples per task")
    sequence_batch_size = config.get("train_sequence_microbatch_size", config["num_generations"])
    if (type(sequence_batch_size) is not int or sequence_batch_size < 1
            or config["num_generations"] % sequence_batch_size):
        raise ValueError("train_sequence_microbatch_size must divide num_generations")
    if "validation_task_limit" in config and (type(config["validation_task_limit"]) is not int or config["validation_task_limit"] < 1):
        raise ValueError("validation_task_limit must be a positive integer when supplied")
    if config["max_prompt_tokens"] + config["max_response_tokens"] > 32768:
        raise ValueError("Configured sequence exceeds Gemma 3 1B's context window")


def main():
    args = argument_parser().parse_args()
    config = json.loads(Path(args.config).read_text())
    validate_config(config)
    validate_run_arguments(args)
    prompt_variant = config.get("prompt_variant", "current")
    constrained_keys = config.get('constrained_parameter_keys', args.mode == 'rollout')
    if constrained_keys and not 0 < config['temperature']:
        raise ValueError('Constrained probabilities require positive sampling temperature')
    verify_probabilities = config.get('verify_constrained_probabilities', False)
    if verify_probabilities and (args.mode != 'rollout' or not constrained_keys):
        raise ValueError('Probability verification requires a constrained rollout')
    if config.get('constrained_compute_dtype') == 'float32' and not verify_probabilities:
        raise ValueError('Float32 compute is currently limited to the probability diagnostic')
    paired_seeds = config.get("rollout_seed_policy") == "paired_episode_v1"
    if args.skip_rollout_episodes and not paired_seeds:
        raise ValueError("Skipping rollout episodes requires paired_episode_v1 seeds")
    if args.mode in TRAINING_MODES and (prompt_variant != "current" or paired_seeds):
        raise ValueError("Prompt comparison options currently support rollout evaluation only")
    if args.mode == "plan":
        print(json.dumps(execution_plan(config), indent=2))
        return
    restore = restore_source(args.restore_checkpoint, args.restore_run, config) if args.restore_checkpoint else None
    client = WorkerClient(config["worker_url"], os.environ.get("ANALOG_WORKER_TOKEN"))
    catalog = client.request("/catalog")
    pilot = {}
    if args.mode == "train":
        if catalog["mode"] != "training":
            raise WorkerError("Training requires a training-mode worker and approved tasks")
    elif args.mode == "research-pilot":
        from training.research_pilot import validate_trainer_manifest
        pilot = validate_trainer_manifest(args.pilot_manifest, config, catalog)
    train_tasks = select_tasks(catalog, "train") if args.mode in TRAINING_MODES else []
    validation_tasks = select_tasks(catalog, "validation", config.get("validation_task_limit")) if args.mode in TRAINING_MODES else []
    selected = select_tasks(catalog, args.split, args.max_tasks) if args.mode == "rollout" else []
    selection = {"policy": "first_sorted_ids_within_unchanged_split", "train_ids": [t["id"] for t in train_tasks],
                 "validation_ids": [t["id"] for t in validation_tasks], "rollout_ids": [t["id"] for t in selected],
                 "validation_catalog_count": sum(t["split"] == "validation" for t in catalog["tasks"]),
                 "validation_task_limit": config.get("validation_task_limit"),
                 "rollout_split": args.split if args.mode == "rollout" else None, "max_tasks": args.max_tasks,
                 "episodes_per_task": args.episodes_per_task if args.mode == "rollout" else None}
    selection["skip_rollout_episodes"] = args.skip_rollout_episodes
    if args.mode == "rollout" and args.skip_rollout_episodes >= len(selected) * args.episodes_per_task:
        raise ValueError("Skipping all selected episodes would produce an empty rollout")
    if config["max_concurrency"] > catalog["max_active"]:
        raise ValueError("Model concurrency exceeds worker episode capacity")

    from training.preflight import check_tpu
    import jax
    if config.get('constrained_compute_dtype') == 'float32':
        jax.config.update('jax_default_matmul_precision', 'highest')
    import jax.numpy as jnp
    import numpy as np
    import optax
    from flax import nnx
    from huggingface_hub import HfApi, snapshot_download
    from etils import epath
    from orbax import checkpoint as ocp
    from tunix.cli.utils.model import apply_lora_to_model
    from tunix.generate.tokenizer_adapter import Tokenizer
    from tunix.models.gemma3 import model as gemma, params_safetensors
    from tunix.rl import rl_cluster as clusters
    from tunix.rl.rollout.base_rollout import RolloutConfig
    from tunix.rl.agentic.agentic_grpo_learner import GRPOConfig, GRPOLearner
    from tunix.rl.agentic.agents.model_agent import ModelAgent
    from tunix.sft import checkpoint_manager
    from tunix.sft.metrics_logger import MetricsLoggerOptions, TensorboardBackend
    from training.tunix_env import CircuitEnvironment

    output = Path(args.output or f"runs/tpu_{args.mode}_{time.time_ns()}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    checkpoint_root = (args.checkpoint_uri.rstrip("/") + "/" + output.name
                       if args.checkpoint_uri else str(output / "checkpoints"))
    if epath.Path(checkpoint_root).exists():
        raise ValueError("Checkpoint prefix already exists; use a unique run name. Resume is not implemented.")
    # Resolve mutable model tags once, then download that exact snapshot and record it.
    hf_token = os.environ.get("HF_TOKEN")
    requested_revision = config["model_revision"]
    revision = (restore["model_revision"] if restore is not None else
                HfApi().model_info(config["model_id"], revision=requested_revision, token=hf_token).sha)
    config["model_revision"] = revision
    run_record = {"config": config, "mode": args.mode, "hardware": None,
                  "generation_seeding": {"policy": "uint32_odd_stride_per_sampler_call_v1",
                      "base_seed": config["seed"], "ledger": "generation_seeds.jsonl",
                      "reproducibility": "Same call order and batching; async scheduling can change seed assignment."},
                  "catalog": catalog, "checkpoint_uri": checkpoint_root, "selection": selection,
                  "requested_model_revision": requested_revision,
                  "plan": execution_plan(config, len(validation_tasks)) if args.mode in TRAINING_MODES else None,
                  "restore": ({key: value for key, value in restore.items() if key != "source_result"}
                              if restore is not None else None), **pilot}
    run_record["prompt"] = {"variant": prompt_variant, "system": system_prompt(prompt_variant),
                            "client_source_sha256": _sha256(Path(__file__).with_name("client.py"))}
    if paired_seeds:
        run_record["generation_seeding"].update(policy="paired_episode_v1",
            reproducibility="SHA256 of base seed, task ID, episode index, and within-episode call; independent of earlier episode lengths.")
    run_path = output / "run.json"
    run_path.write_text(json.dumps(run_record, indent=2) + "\n")
    (output / "requirements-resolved.txt").write_text(subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True))
    run_record['constrained_parameter_keys'] = constrained_keys
    if constrained_keys:
        run_record['constrained_decoder_source_sha256'] = _sha256(Path(__file__).with_name('constrained_json.py'))
        run_record['constrained_training_source_sha256'] = _sha256(Path(__file__).with_name('constrained_training.py'))
        run_record['sampling_distribution'] = {'grammar_masked': True, 'top_k': 0, 'top_p': 1.0,
                                              'temperature': config['temperature']}
    snapshot = snapshot_download(config["model_id"], revision=revision, token=hf_token,
                                 allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt"])
    tokenizer = Tokenizer("huggingface", snapshot, add_bos=False, add_eos=False, hf_access_token=hf_token)
    run_record['chat_format_check'] = verify_chat_contract(tokenizer)
    run_record['chat_format_source_sha256'] = _sha256(Path(__file__).with_name('chat_format.py'))
    # Refuse a broken chat format before touching TPU devices or loading weights.
    run_record['hardware'] = check_tpu()
    run_path.write_text(json.dumps(run_record, indent=2) + '\n')
    mesh = jax.sharding.Mesh(np.array(jax.devices()).reshape(1, -1), ("fsdp", "tp"))
    # Gemma 1B has one KV head. Tunix 0.1.7 shares act_btnh between
    # query and KV activations, so that head axis cannot be split over TP.
    model_sharding = replace(gemma.ShardingConfig.get_default_sharding(),
                             act_btnh=("fsdp", None, None, None))
    with jax.set_mesh(mesh):
        reference = params_safetensors.create_model_from_safe_tensors(
            snapshot, gemma.ModelConfig.gemma3_1b_it(sharding_config=model_sharding), mesh, dtype=jnp.bfloat16)
        actor = apply_lora_to_model(reference, mesh, config["lora"])
    adapter_leaves = jax.tree_util.tree_leaves(nnx.state(actor, nnx.LoRAParam))
    if not adapter_leaves:
        raise RuntimeError("No LoRA parameters found; refusing an accidental full-model training run")

    def adapter_digest(model):
        checksum = hashlib.sha256()
        for leaf in jax.tree_util.tree_leaves(nnx.state(model, nnx.LoRAParam)):
            value = np.asarray(jax.device_get(leaf), dtype=np.float32)
            if not np.isfinite(value).all():
                raise RuntimeError("Non-finite LoRA weights; inspect loss and optimizer stability")
            checksum.update(value.tobytes())
        return checksum.hexdigest()

    initial_adapter_sha256 = adapter_digest(actor)

    def read_checkpoint_manager(root):
        if not epath.Path(root).is_dir():
            raise FileNotFoundError("Actor checkpoint directory is missing: " + root)
        return checkpoint_manager.CheckpointManager(root_directory=root,
            options=ocp.CheckpointManagerOptions(read_only=True, create=False))

    if restore is not None:
        run_record["restore"].update(restore_adapter(actor, restore, adapter_digest, read_checkpoint_manager))
        run_path.write_text(json.dumps(run_record, indent=2) + "\n")
    if config.get('constrained_compute_dtype') == 'float32':
        # Explicit diagnostic option: preserve numeric weights while removing
        # bfloat16 activation/cache rounding from sampler/teacher comparisons.
        for model in (actor, reference):
            state = nnx.state(model)
            state = jax.tree.map(lambda x: x.astype(jnp.float32)
                                if hasattr(x, 'dtype') and jnp.issubdtype(x.dtype, jnp.floating) else x, state)
            nnx.update(model, state)
    chat_parser = GemmaChatParser(tokenizer, config["max_prompt_tokens"])
    cluster_config = clusters.ClusterConfig(
        role_to_mesh={role: mesh for role in (clusters.Role.ACTOR, clusters.Role.REFERENCE, clusters.Role.ROLLOUT)},
        rollout_engine="vanilla", offload_to_cpu=False,
        training_config=clusters.RLTrainingConfig(
            actor_optimizer=optax.chain(optax.clip_by_global_norm(1.0), optax.adamw(config["learning_rate"])),
            max_steps=config["max_updates"], mini_batch_size=1, train_micro_batch_size=1,
            eval_every_n_steps=config["eval_every"],
            checkpoint_root_directory=checkpoint_root if args.mode in TRAINING_MODES else None,
            checkpointing_options=ocp.CheckpointManagerOptions(save_interval_steps=config["checkpoint_every"], max_to_keep=3),
            metrics_logging_options=MetricsLoggerOptions(
                log_dir=str(output / "metrics"), flush_every_n_steps=1,
                backend_kwargs={"custom_backend": [lambda: TensorboardBackend(
                    log_dir=str(output / "metrics"), flush_every_n_steps=1)]})),
        rollout_config=RolloutConfig(
            max_tokens_to_generate=config["max_response_tokens"], max_prompt_length=config["max_prompt_tokens"],
            kv_cache_size=config["max_prompt_tokens"] + config["max_response_tokens"] + 256,
            temperature=config["temperature"], top_p=1.0, top_k=0 if constrained_keys else 50,
            # Sampler donates its state, including the key. An integer lets it
            # create a fresh key per call instead of reusing a deleted array.
            eos_tokens=[1, 106], return_logprobs=True, seed=config["seed"]))
    sequence_batch_size = config.get("train_sequence_microbatch_size", config["num_generations"])
    # RLTrainingConfig counts prompt groups, but we split sequences after group
    # advantages are computed. Set the trainer's accumulation count accordingly.
    cluster_config.training_config.gradient_accumulation_steps = config["num_generations"] // sequence_batch_size
    binding = {"source_run_sha256": _sha256(run_path), "model_id": config["model_id"],
               "model_revision": revision, "lora_config_sha256": _canonical_sha256(config["lora"])}
    training_result = None
    cluster = clusters.RLCluster(actor=actor, reference=reference, tokenizer=tokenizer, cluster_config=cluster_config)
    if args.mode in TRAINING_MODES and sequence_batch_size < config["num_generations"]:
        install_sequence_accumulation(cluster, config["num_generations"], sequence_batch_size)
    with closing_cluster(cluster, save_final_checkpoint if args.mode in TRAINING_MODES else None), ExitStack() as training_scope:
        generator = cluster.rollout.generate
        if constrained_keys and args.mode in TRAINING_MODES:
            from training.constrained_training import Constraints, ConstrainedGeneration, training_probabilities
            constraints = Constraints(tokenizer)
            generator = ConstrainedGeneration(cluster.rollout, constraints)
            GRPOLearner = training_scope.enter_context(training_probabilities(cluster, constraints, config['temperature']))
        cluster.rollout.generate = SeededGeneration(
            generator, config["seed"], output / "generation_seeds.jsonl",
            output / "generations.jsonl",
            alignment_check=lambda prompts, result: completion_alignment(tokenizer, prompts, result),
            require_alignment=args.mode in TRAINING_MODES)
        if args.mode in TRAINING_MODES:
            trainer = cluster.actor_trainer
            original_metadata = trainer.custom_checkpoint_metadata
            trainer.custom_checkpoint_metadata = lambda: {
                **original_metadata(), **binding, "checkpoint_step": int(trainer.train_steps)}
        if args.mode == "rollout":
            key_grammar = None
            if verify_probabilities:
                from training.constrained_training import Constraints, verify_rollout_probabilities
                constraints = Constraints(tokenizer)
            with (output / "rollouts.jsonl").open("w") as stream:
                for task, episode_index in rollout_requests(selected, args.episodes_per_task, args.skip_rollout_episodes):
                    if paired_seeds:
                        cluster.rollout.generate.set_episode(task["id"], episode_index)
                    episode = RemoteEpisode(client, task["id"], max_steps=config["max_episode_steps"], required_mode=catalog["mode"],
                                            trajectory_directory=output / "trajectories")
                    try:
                        initial_observation = episode.reset()
                        if constrained_keys:
                            from training.constrained_json import install
                            names = tuple(sorted(json.loads(initial_observation)['parameters']))
                            if key_grammar is None or key_grammar.names != names:
                                key_grammar = install(cluster.rollout, tokenizer, names)
                        messages = [{"role": "system", "content": system_prompt(prompt_variant)},
                                    {"role": "user", "content": action_prompt(initial_observation, prompt_variant)}]
                        for step in range(config["max_episode_steps"]):
                            prompt = chat_parser.parse(messages, add_generation_prompt=True, is_first_msg=True)
                            result = cluster.generate(prompts=[prompt])
                            if verify_probabilities:
                                check = verify_rollout_probabilities(cluster, constraints, result, config['temperature'])
                                with (output / 'probability_checks.jsonl').open('a') as checks:
                                    checks.write(json.dumps({'task_id': task['id'], 'episode': episode_index,
                                                            'step': step + 1, **check}) + '\n')
                                if not check['passed']:
                                    raise RuntimeError('Constrained sampler/trainer probability check failed; see probability_checks.jsonl')
                            response = result.text[0]
                            if key_grammar is not None:
                                # Fail closed on truncation/decoder mismatch before evaluator use.
                                # Raw generation is already saved by SeededGeneration.
                                key_grammar.validate(response)
                            observation, reward, done, info = episode.step(response)
                            stream.write(json.dumps({"task_id": task["id"], "episode": episode_index,
                                                     "step": step + 1, "response": response,
                                                     "observation": observation, "reward_delta": reward, **info}) + "\n")
                            stream.flush()
                            messages += [{"role": "assistant", "content": response},
                                         {"role": "user", "content": action_prompt(observation, prompt_variant)}]
                            if done:
                                break
                    except PromptWindowExceeded as exc:
                        error = {"task_id": task["id"], "episode": episode_index,
                                 "kind": "context_overflow", "completed_attempts": episode.steps,
                                 "last_observed_score": episode.previous_score, "error": str(exc)}
                        with (output / "episode_errors.jsonl").open("a") as error_stream:
                            error_stream.write(json.dumps(error) + "\n")
                        print("Episode stopped at prompt limit: " + json.dumps(error), flush=True)
                    finally:
                        episode.close()
        else:
            learner = GRPOLearner(
                rl_cluster=cluster, reward_fns=None, chat_parser=chat_parser,
                algo_config=GRPOConfig(num_generations=config["num_generations"], num_iterations=1,
                                       beta=config["beta"], epsilon=0.2, system_prompt=SYSTEM_PROMPT,
                                       max_response_length=config["max_response_tokens"],
                                       max_concurrency=config["max_concurrency"], off_policy_steps=0,
                                       episode_timeout=1800.0, overlong_filter=True),
                agent_class=ModelAgent, env_class=CircuitEnvironment,
                env_kwargs={"endpoint": config["worker_url"], "max_steps": config["max_episode_steps"],
                            "trajectory_directory": str(output / "trajectories"),
                            "required_mode": "research-pilot" if args.mode == "research-pilot" else "training"})
            validation = [{"prompts": ["Solve the circuit sizing task using the simulator."], "task_id": [t["id"]]}
                          for t in validation_tasks]
            learner.train(batches(train_tasks, config["max_updates"], config["seed"]), eval_dataset=validation)
            steps = int(cluster.global_steps)
            if steps < 1:
                raise RuntimeError("No optimizer steps completed; inspect rollout truncation, failures, and reward variation")
            final_adapter_sha256 = adapter_digest(cluster.actor_trainer.model)
            if final_adapter_sha256 == initial_adapter_sha256:
                raise RuntimeError("Optimizer ran but LoRA weights did not change; inspect group reward variation")
            training_result = {"optimizer_steps": steps, "checkpoint_step": int(cluster.actor_trainer.train_steps),
                               "initial_adapter_sha256": initial_adapter_sha256,
                               "final_adapter_sha256": final_adapter_sha256,
                               "device_memory": [d.memory_stats() for d in jax.devices()],
                               "checkpoint_root": checkpoint_root, **pilot}
    if training_result is not None:
        # Cluster close completes pending saves. Reopen read-only and compare the
        # persisted LoRA parameters before claiming a successfully saved run.
        source = {**binding, "source_run": str(run_path), "checkpoint_root": checkpoint_root,
                  "source_result": training_result}
        training_result["checkpoint_readback"] = restore_adapter(
            cluster.actor_trainer.model, source, adapter_digest, read_checkpoint_manager)
        (output / "training_result.json").write_text(json.dumps(training_result, indent=2) + "\n")
    print(f"Completed {args.mode}: {output}")


if __name__ == "__main__":
    main()
