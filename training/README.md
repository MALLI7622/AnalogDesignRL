# TPU training setup

Status, 2026-09-10: CPU service and real ngspice integration tested locally. TPU trainer and Ubuntu bootstrap are implemented but have not run on a TPU. No cloud resources or paid training runs have been started. Current tasks still need training approval.

## Starting choice

**Gemma 3 1B-IT, LoRA, JAX/Flax, and Tunix 0.1.7.** Google publishes a [Gemma 3 1B GRPO example on one v6e TPU](https://tunix.readthedocs.io/en/stable/_collections/examples/grpo_gemma.html). This makes it a practical integration starting point; its analog-design ability still needs measurement. GPT-6/Codex remains the task generator; Gemma is the trainable solver.

The [model weights](https://huggingface.co/google/gemma-3-1b-it) require accepting Gemma's terms in your Hugging Face account. `HF_TOKEN` allows downloading them; it is not a paid inference API key. The trainer resolves the model revision to a commit and records it before downloading.

## Architecture and resources

```text
TPU: Gemma proposes numeric edits → host CPU: ngspice evaluates edits
               ↑                            ↓
         next design attempt ← measurements and constraint failures

After a group of episodes: verifier scores → GRPO → LoRA weight update
```

ngspice runs as an ordinary CPU process. It does not need to be differentiable: the RL update uses the score and the model's token probabilities. We do not train on simulator internals.

| Resource | Initial allocation |
|---|---|
| TPU | One v6e-1, 32 GB HBM. Move to v6e-4 if the measured memory footprint requires it. |
| CPU | Four simulator workers on the TPU VM's host CPUs; reserve other cores for JAX. |
| Storage | Start with a 200 GB boot disk and a private Cloud Storage bucket for checkpoints/artifacts. |
| Software | Ubuntu TPU image, Python 3.12, Tunix 0.1.7, ngspice 47, pinned circuit/device-model dependencies. |
| Access | Allocated TPU type/zone/quota, enabled Compute API, SSH access, service account with access to the experiment bucket. |

Google lists [32 GB HBM per v6e chip](https://docs.cloud.google.com/tpu/docs/v6e). A 1B BF16 weight set alone is about 2 GB; reference weights, activations, token logits, KV cache, and optimizer state add memory. The official short-prompt example is not a memory guarantee for our longer episodes. This starter supports one host with 1, 2, or 4 JAX TPU devices; multi-host execution is deferred.

The service binds to `127.0.0.1`, authenticates requests, accepts only catalog task IDs and parameter actions, and limits concurrency. References, testbench files, and raw errors are not returned to the model. No generated Python, shell, SPICE, or arbitrary tool calls are executed. Moving workers to a separate CPU VM only requires an SSH tunnel and the same API; no public HTTP service is needed. This API restriction does not create an OS sandbox around the trusted trainer process.

## Local checks

Run from the repository root, after the existing simulator setup:

```sh
python3 -m training.preflight
python3 -m unittest discover -s tests -q
python3 -m training.smoke
python3 -m training.train --mode plan
```

The smoke check uses two public installation fixtures, first failing and then passing, over the real HTTP service. It tests concurrent episodes, termination, reward totals, and retry handling. It does not run or train an LLM. Reports and raw simulations go under `runs/worker_smoke_*/`.

## Provision and install

Copy `training/cloud.example.json` to ignored `training/cloud.local.json`. Fill in the TPU Builders project, allocated zone/type, bucket region/name, and an existing service account. Credits do not by themselves establish quota or capacity. The currently configured local gcloud project is not automatically selected for this research.

```sh
python3 -m training.cloud_plan --config training/cloud.local.json
```

This **only prints** inspection, provisioning, connection, and cleanup commands. First confirm quota and the offered machine type. The commands follow Google's [Compute Engine TPU guide](https://docs.cloud.google.com/compute/docs/tpus/create-tpu-vm-instance); programme-provided reservations or older TPU types may require a different creation command. The caller needs permissions to create instances and use the selected service account. Bucket IAM is scoped to that bucket.

On the allocated TPU VM, clone the project:

```sh
git clone https://github.com/MALLI7622/AnalogDesignRL.git
cd AnalogDesignRL
```

The clone includes source code, manual task fixtures, the six pilot task JSONs, their index, and episode summaries. Additional generated batches, private references, and raw simulation artifacts stay outside Git. Copy any additional task catalog and task JSONs needed for your experiment. Keep credentials in the VM environment. Run there:

```sh
bash training/bootstrap.sh
source .venv-tpu/bin/activate
export PATH="$PWD/.deps/ngspice-47/bin:$PATH"
```

Bootstrap installs Python and TPU dependencies, builds checksum-pinned ngspice 47, downloads the project's pinned device models, and runs verification. It changes the VM's packages and downloads dependencies. The requirement file pins Tunix and selected compatibility dependencies; it is **not yet a tested, fully resolved TPU lockfile**. Bootstrap writes the exact installed versions under `runs/environment/`. Freeze and reuse those versions after the first successful TPU run.

Create a worker token once, with restricted permissions:

```sh
mkdir -p .cache
python3 -c 'import os,secrets; p=".cache/worker.token"; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_urlsafe(32).encode()); os.close(fd)'
export ANALOG_WORKER_TOKEN="$(cat .cache/worker.token)"
```

In terminal one, start the evaluation worker:

```sh
python3 -m training.worker --catalog training/configs/smoke_tasks.json \
  --output runs/evaluation_worker --mode evaluation --workers 4
```

In terminal two, activate the same environment, export the same worker token, and set `HF_TOKEN` privately or use your saved Hugging Face login. Run the actual model without changing its weights:

```sh
python3 -m training.train --mode rollout --split smoke --output runs/gemma_initial_rollout
```

This is the next hardware validation: load weights, generate JSON, consume simulator feedback, and record complete attempts in `rollouts.jsonl`. This diagnostic is not yet a matched scientific baseline: Tunix's training response limit counts intermediate environment messages as well as model tokens. A model that cannot produce valid actions may need a small supervised warm-up before RL.

## Training and reward

`training/configs/gemma3_1b.json` starts with four attempts per episode, four samples of one task per update, LoRA rank 16, and 20 updates. The four-attempt cap is explicitly shown to the model; the underlying task's 30-evaluation definition stays unchanged. Increase the cap and context buckets together after profiling. Prompt overflow aborts the run instead of silently dropping requirements. Tunix filters overlong training trajectories; inspect truncation rates before trusting a run.

The simulator score is unchanged: +1 if all constraints pass; otherwise a negative violation score or -1 for invalid/failed evaluations. Tunix sums step rewards, so the adapter sends `current_score - previous_score`, starting from zero. For scores `[-0.5, -0.2, 1]`, it sends `[-0.5, 0.3, 1.2]`; the episode total is exactly +1. GRPO compares these episode totals within each task's group. There is no extra reward merely for producing valid JSON. Network/service errors raise exceptions instead of becoming model penalties.

Actual training needs a reviewed catalog:

1. Generate a catalog with `python3 -m training.catalog --tasks TASK_JSONS --output CATALOG_JSON`. It starts in `smoke` mode.
2. Assign `train`, `validation`, and eventually a final `test` split. Keep whole topologies together for structural generalization. Generate the catalog with `--split-policy requirements` for familiar-topology studies while keeping matching conditions/constraints together. Do not describe those studies as structural generalization.
3. Complete the existing review records in `verification/qualification.json`. Hashes bind approval to the task and evaluator; changing a split label does not approve a task. Do not treat the six current pilot tasks as sufficient training diversity.
4. Start the worker with the reviewed catalog and `--mode training`, then run:

```sh
python3 -m training.train --mode train --output runs/gemma_experiment_001 \
  --checkpoint-uri gs://YOUR_BUCKET/checkpoints
```

The trainer refuses an evaluation worker and requires train/validation splits. It uses the environment's scores, keeps simulator feedback out of the token loss via Tunix's agentic masks, and saves configuration, resolved model revision, catalog hashes, dependency versions, metrics, and LoRA checkpoints. Cloud checkpoints use a separate prefix for each local run name. Use a unique run name. Checkpoint restore and interruption recovery still need a TPU test before using preemptible capacity; the CLI does not yet offer resume.

## Runtime and experiment checks

Local smoke measurement: four candidate evaluations / eight ngspice invocations, about **1.25 seconds per evaluation**, 3.33 seconds total with two concurrent episodes, and 3.55 MB of artifacts. This is a tiny macOS sample, not TPU-host throughput. Rerun it on the VM.

Upper bound per update: `tasks_per_batch × samples_per_task × attempts`. Here that is `1 × 4 × 4 = 16` evaluations, normally up to 32 ngspice invocations. Twenty updates allow up to 320 evaluations / 640 invocations, plus validation and baseline runs. With four CPU workers and the local mean, simulation work alone has an ideal lower bound near five seconds per update. Compilation, model generation/backpropagation, slow circuits, queues, and storage add time. Each ngspice invocation has a 30-second timeout.

Before scaling: verify finite loss and changed LoRA weights on approved tasks, restore one checkpoint, measure peak HBM and step time, inspect invalid-action/timeouts/context truncation, and check that group scores vary. Identical scores produce no useful relative reward signal. Compare success rate and simulator calls against the untrained model and seeded random search under identical budgets, then add an established sizing optimizer and multiple seeds. High training reward alone is not evidence of generalization.

Copy completed run artifacts to the private bucket before deleting compute, for example `gcloud storage rsync --recursive runs/EXPERIMENT gs://YOUR_BUCKET/runs/EXPERIMENT`. Keep full logs during the pilot; disk usage grows with every simulation. Final experiment cost must include TPU time, host/extra CPU resources, disks, and object storage according to what the programme credits cover.
