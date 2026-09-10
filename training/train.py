"""Gemma 3 1B LoRA GRPO on a TPU, with ngspice behind the worker API."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

from training.client import RemoteEpisode, SYSTEM_PROMPT, WorkerClient, WorkerError


def batches(tasks, count, seed):
    rng = random.Random(seed)
    for _ in range(count):
        task = rng.choice(tasks)
        # Tunix constructs one environment per group member using this task ID.
        yield {"prompts": ["Solve the circuit sizing task using the simulator."], "task_id": [task["id"]]}


def validate_config(config):
    if config["model_id"] != "google/gemma-3-1b-it":
        raise ValueError("This loader implements Gemma 3 1B-IT; another architecture needs its own loader")
    for key in ("max_episode_steps", "max_prompt_tokens", "max_response_tokens", "max_updates",
                "num_generations", "max_concurrency", "checkpoint_every", "eval_every"):
        if isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if config["num_generations"] < 2:
        raise ValueError("GRPO needs at least two samples per task")
    if config["max_prompt_tokens"] + config["max_response_tokens"] > 32768:
        raise ValueError("Configured sequence exceeds Gemma 3 1B's context window")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--config", default="training/configs/gemma3_1b.json")
    cli.add_argument("--mode", choices=["plan", "rollout", "train"], default="plan")
    cli.add_argument("--output", default=None, help="New local run directory")
    cli.add_argument("--checkpoint-uri", help="Optional dedicated gs://... checkpoint prefix")
    cli.add_argument("--split", choices=["smoke", "train", "validation", "test"], default="smoke", help="Rollout mode only")
    args = cli.parse_args()
    config = json.loads(Path(args.config).read_text())
    validate_config(config)
    if args.checkpoint_uri and not args.checkpoint_uri.startswith("gs://"):
        raise ValueError("--checkpoint-uri must be a gs:// bucket prefix")
    if args.mode == "plan":
        print(json.dumps({"model": config["model_id"], "algorithm": "LoRA + multi-turn GRPO",
                          "updates": config["max_updates"], "episodes_per_update": config["num_generations"],
                          "max_evaluations_per_episode": config["max_episode_steps"],
                          "max_training_simulator_invocations": 2 * config["max_updates"] * config["num_generations"] * config["max_episode_steps"],
                          "note": "Upper bound excludes validation/baselines. No weights downloaded or compute created."}, indent=2))
        return
    client = WorkerClient(config["worker_url"], os.environ.get("ANALOG_WORKER_TOKEN"))
    catalog = client.request("/catalog")
    if args.mode == "train":
        if catalog["mode"] != "training":
            raise WorkerError("Training requires a training-mode worker and approved tasks")
        for split in ("train", "validation"):
            if not any(t["split"] == split for t in catalog["tasks"]):
                raise ValueError(f"Frozen catalog needs a {split} split")
    if config["max_concurrency"] > catalog["max_active"]:
        raise ValueError("Model concurrency exceeds worker episode capacity")

    from training.preflight import check_tpu
    hardware = check_tpu()
    import jax
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
    from tunix.rl.agentic.parser.chat_template_parser.parser import GemmaChatTemplateParser
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
    revision = HfApi().model_info(config["model_id"], revision=config["model_revision"], token=hf_token).sha
    config["model_revision"] = revision
    (output / "run.json").write_text(json.dumps({"config": config, "mode": args.mode, "hardware": hardware,
                                                "catalog": catalog, "checkpoint_uri": checkpoint_root}, indent=2) + "\n")
    (output / "requirements-resolved.txt").write_text(subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True))
    snapshot = snapshot_download(config["model_id"], revision=revision, token=hf_token,
                                 allow_patterns=["*.json", "*.safetensors", "*.model", "*.txt"])
    mesh = jax.sharding.Mesh(np.array(jax.devices()).reshape(1, -1), ("fsdp", "tp"))
    with mesh:
        reference = params_safetensors.create_model_from_safe_tensors(
            snapshot, gemma.ModelConfig.gemma3_1b_it(), mesh, dtype=jnp.bfloat16)
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
    tokenizer = Tokenizer("huggingface", snapshot, add_bos=False, add_eos=False, hf_access_token=hf_token)

    class CheckedParser(GemmaChatTemplateParser):
        def parse(self, messages, **kwargs):
            # The upstream Gemma parser merges the system message into the first
            # user's dictionary. Preserve the agent's original conversation.
            result = super().parse(copy.deepcopy(messages), **kwargs)
            if len(self.tokenizer.encode(result)) > config["max_prompt_tokens"]:
                raise RuntimeError("Conversation exceeds prompt bucket; increase max_prompt_tokens before continuing")
            return result

    chat_parser = CheckedParser(tokenizer)
    cluster_config = clusters.ClusterConfig(
        role_to_mesh={role: mesh for role in (clusters.Role.ACTOR, clusters.Role.REFERENCE, clusters.Role.ROLLOUT)},
        rollout_engine="vanilla", offload_to_cpu=False,
        training_config=clusters.RLTrainingConfig(
            actor_optimizer=optax.chain(optax.clip_by_global_norm(1.0), optax.adamw(config["learning_rate"])),
            max_steps=config["max_updates"], mini_batch_size=1, train_micro_batch_size=1,
            eval_every_n_steps=config["eval_every"], checkpoint_root_directory=checkpoint_root,
            checkpointing_options=ocp.CheckpointManagerOptions(save_interval_steps=config["checkpoint_every"], max_to_keep=3),
            metrics_logging_options=MetricsLoggerOptions(
                log_dir=str(output / "metrics"), flush_every_n_steps=1,
                backend_kwargs={"custom_backend": [lambda: TensorboardBackend(
                    log_dir=str(output / "metrics"), flush_every_n_steps=1)]})),
        rollout_config=RolloutConfig(
            max_tokens_to_generate=config["max_response_tokens"], max_prompt_length=config["max_prompt_tokens"],
            kv_cache_size=config["max_prompt_tokens"] + config["max_response_tokens"] + 256,
            temperature=config["temperature"], top_p=1.0, top_k=50,
            eos_tokens=[1, 106], return_logprobs=True, seed=jax.random.PRNGKey(config["seed"])))
    cluster = clusters.RLCluster(actor=actor, reference=reference, tokenizer=tokenizer, cluster_config=cluster_config)
    if args.mode == "rollout":
        selected = [t for t in catalog["tasks"] if t["split"] == args.split]
        if not selected:
            raise ValueError("No tasks in the requested rollout split")
        try:
            with (output / "rollouts.jsonl").open("w") as stream:
                for task in selected:
                    episode = RemoteEpisode(client, task["id"], max_steps=config["max_episode_steps"], required_mode=catalog["mode"])
                    try:
                        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": episode.reset()}]
                        for step in range(config["max_episode_steps"]):
                            prompt = chat_parser.parse(messages, add_generation_prompt=True, is_first_msg=True)
                            result = cluster.generate(prompts=[prompt])
                            response = result.text[0]
                            observation, reward, done, info = episode.step(response)
                            stream.write(json.dumps({"task_id": task["id"], "step": step + 1, "response": response,
                                                     "observation": observation, "reward_delta": reward, **info}) + "\n")
                            stream.flush()
                            messages += [{"role": "assistant", "content": response}, {"role": "user", "content": observation}]
                            if done:
                                break
                    finally:
                        episode.close()
        finally:
            cluster.close()
    else:
        learner = GRPOLearner(
            rl_cluster=cluster, reward_fns=None, chat_parser=chat_parser,
            algo_config=GRPOConfig(num_generations=config["num_generations"], num_iterations=1,
                                   beta=config["beta"], epsilon=0.2, system_prompt=SYSTEM_PROMPT,
                                   max_response_length=config["max_response_tokens"],
                                   max_concurrency=config["max_concurrency"], off_policy_steps=0,
                                   episode_timeout=1800.0, overlong_filter=True),
            agent_class=ModelAgent, env_class=CircuitEnvironment,
            env_kwargs={"endpoint": config["worker_url"], "max_steps": config["max_episode_steps"]})
        train_tasks = [t for t in catalog["tasks"] if t["split"] == "train"]
        validation = [{"prompts": ["Solve the circuit sizing task using the simulator."], "task_id": [t["id"]]}
                      for t in catalog["tasks"] if t["split"] == "validation"]
        learner.train(batches(train_tasks, config["max_updates"], config["seed"]), eval_dataset=validation)
        steps = cluster.global_steps
        if steps < 1:
            raise RuntimeError("No optimizer steps completed; inspect rollout truncation, failures, and reward variation")
        final_adapter_sha256 = adapter_digest(cluster.actor_trainer.model)
        if final_adapter_sha256 == initial_adapter_sha256:
            raise RuntimeError("Optimizer ran but LoRA weights did not change; inspect group reward variation")
        (output / "training_result.json").write_text(json.dumps({"optimizer_steps": steps,
                                                                "initial_adapter_sha256": initial_adapter_sha256,
                                                                "final_adapter_sha256": final_adapter_sha256,
                                                                "device_memory": [d.memory_stats() for d in jax.devices()],
                                                                "checkpoint_root": checkpoint_root}, indent=2) + "\n")
    print(f"Completed {args.mode}: {output}")


if __name__ == "__main__":
    main()
