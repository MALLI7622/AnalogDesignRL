# TPU training setup

Status, 2026-09-16: Ubuntu bootstrap, simulator verification, HTTP smoke tests, and JAX detection of four v5e devices passed on the existing TPU VM. Gemma weights are downloaded. Model rollout, optimizer updates, and real checkpoint restoration remain unverified. Current tasks still need independent training approval.

For the prepared two-update implementation on the existing VM, follow the [research-pilot launch guide](RESEARCH_PILOT_README.md). Its separately authorized mode preserves the ordinary training qualification gate. The provisioning examples below describe the original v6e plan, not the existing v5e allocation.

For the completed 250-task dataset, use the [training-to-AnalogGym experiment guide](../TRAINING_ANALOGGYM_README.md) alongside this infrastructure guide. It specifies the 174/21/55 split, circuit overlap, matched evaluation, and remaining integration work. Checkpoint loading for rollout is now implemented with provenance/hash checks; actual hardware validation remains pending.

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
| TPU | One `ct6e-standard-4t` VM: four v6e chips, 32 GB HBM each. Use the programme's FLEX_START capacity. |
| CPU | Four simulator workers on the TPU VM's host CPUs; reserve other cores for JAX. |
| Storage | 50 GB disposable boot disk, 200 GB retained Hyperdisk Balanced for the workspace, private Cloud Storage bucket for backups. |
| Software | Ubuntu TPU image, Python 3.12, Tunix 0.1.7, ngspice 47, pinned circuit/device-model dependencies. |
| Access | Confirmed programme credits, regional v6e Flex quota, Compute/TPU APIs, SSH access, service account with access to the experiment bucket. |

Google lists [32 GB HBM per v6e chip](https://docs.cloud.google.com/tpu/docs/v6e). A 1B BF16 weight set alone is about 2 GB; reference weights, activations, token logits, KV cache, and optimizer state add memory. The official short-prompt example is not a memory guarantee for our longer episodes. This starter supports one host with 1, 2, or 4 JAX TPU devices; multi-host execution is deferred.

The programme guide lists four-chip v6e capacity, so that is our provisioning preset. Its host has [180 vCPUs and 720 GB RAM](https://docs.cloud.google.com/tpu/docs/v6e); start with simulations on that host before buying another CPU VM. This is a setup choice, not a measured throughput optimum.

Start with a **four-hour run limit** and a **two-hour queue limit**. Google's [Flex list price](https://cloud.google.com/products/dws/pricing) on 2026-09-10 is $1.35 per v6e chip-hour: **$5.40/hour, or $21.60 for four hours**, plus disks, object storage, and networking. Credits must be active and cover the relevant SKUs. Budget alerts notify you; the VM's run limit ends compute. Retained storage keeps billing until deleted.

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

These defaults follow the supplied **TPU Builders Getting Started Guide**, updated July 24, 2026. Keep that document local. Before provisioning, confirm that the final credits application was submitted, credits appear on the linked billing account, and a project budget has alerts at 50%, 75%, and 90%.

Copy `training/cloud.example.json` to ignored `training/cloud.local.json`. Fill in the credited project, your Google account email, a unique private bucket name, and an existing VM service account email. The default zone is `us-east5-a`. Other v6e zones listed by the programme are `us-east5-b`, `us-central1-a`, `europe-west4-a`, and `southamerica-west1-a`; set `region` to match the chosen zone. Quota and current capacity still need checking. Do not create requests in multiple zones simultaneously.

```sh
cp training/cloud.example.json training/cloud.local.json
# Edit training/cloud.local.json, then print the read-only checks:
python3 -m training.cloud_plan --config training/cloud.local.json --phase inspect
```

The planner **prints commands; it never executes them**. Review and run each phase's output individually from this repository root. All commands select the project and account explicitly. Do not pipe `--phase all` into a shell: it includes cleanup.

Check billing, the service account, and regional v6e Flex/preemptible quota sufficient for four chips. If Compute is disabled, run the `enable` phase after checking the project and billing, then repeat inspection. The guide also flags `GPUS_ALL_REGIONS=0` as a programme onboarding issue; [public Compute quota documentation](https://docs.cloud.google.com/compute/resource-usage) lists TPU quotas separately, so a nonzero GPU quota alone does not establish TPU access. Ask programme support about conflicting quota/backend errors.

```sh
# Print API setup, then one-time private bucket and retained disk creation:
python3 -m training.cloud_plan --config training/cloud.local.json --phase enable
python3 -m training.cloud_plan --config training/cloud.local.json --phase storage
# First boot of the newly created blank data disk:
python3 -m training.cloud_plan --config training/cloud.local.json --phase provision --initialize-data-disk
# Inspect status/startup logs and connect after the VM is running:
python3 -m training.cloud_plan --config training/cloud.local.json --phase connect
```

This uses the guide's Compute Engine v6e route; `gcloud alpha` and legacy v5e queued resources are unnecessary. The caller needs permission to create instances/disks/buckets and use the selected service account. Bucket access is scoped to that bucket. The VM expires after four running hours and is deleted; its data disk has `auto-delete=no`. The queue can wait up to two hours before that running period starts. FLEX_START has a [seven-day maximum](https://docs.cloud.google.com/tpu/docs/create-flex-start-compute); keep the short limit while debugging. Spot and multi-host setups can follow after checkpoint recovery works.

The startup script mounts the data disk at `/mnt/data`. It formats a blank disk only with the explicit initialization flag and refuses unknown filesystem signatures. In the SSH session, confirm the mount **before** creating the workspace:

```sh
mountpoint /mnt/data
# Continue only if the mount exists:
sudo install -d -o "$USER" -g "$(id -gn)" -m 700 /mnt/data/analog-rl
cd /mnt/data/analog-rl
git clone https://github.com/MALLI7622/AnalogDesignRL.git
cd AnalogDesignRL
```

The clone includes source code, manual fixtures, the six pilot task JSONs, their index, and episode summaries. Copy any additional catalog/task files needed for your experiment. On later VMs, reuse the same disk and path, omit `--initialize-data-disk`, and update the existing clone with `git pull --ff-only`. The disk is zonal: moving zones needs a separate storage migration.

```sh
bash training/bootstrap.sh
source training/activate.sh
```

Bootstrap installs host packages, Python 3.12, TPU dependencies, checksum-pinned ngspice 47, and pinned device models, then runs verification and HTTP smoke checks. Subsequent boots reuse the environment and simulator build. Exact package versions and input hashes are saved under `runs/environment/`; if the environment is missing and inputs are unchanged, bootstrap reinstalls from that resolved list. The initial dependency resolution and Ubuntu/TPU runtime still need hardware validation.

Source `training/activate.sh` in each shell. It keeps Hugging Face downloads, Python/uv files, and a bounded 20 GiB [JAX compilation cache](https://docs.jax.dev/en/latest/persistent_compilation_cache.html) on the data disk, limits BLAS thread oversubscription, and loads an existing worker token. Keep the workspace path unchanged when reusing Python environments. Compilation cache hits depend on matching software, hardware, and shapes.

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

The trainer refuses an evaluation worker and requires train/validation splits. It uses the environment's scores, keeps simulator feedback out of the token loss via Tunix's agentic masks, and saves configuration, resolved model revision, catalog hashes, dependency versions, metrics, and LoRA checkpoints. Cloud checkpoints use a separate prefix for each local run name. Use a unique run name. The CLI does not yet implement resume: retained files preserve evidence and checkpoints, but do not automatically restore a training session. Implement and test recovery before longer or Spot runs.

## Runtime and experiment checks

Local smoke measurement: four candidate evaluations / eight ngspice invocations, about **1.25 seconds per evaluation**, 3.33 seconds total with two concurrent episodes, and 3.55 MB of artifacts. This is a tiny macOS sample, not TPU-host throughput. Rerun it on the VM.

Upper bound per update: `tasks_per_batch × samples_per_task × attempts`. Here that is `1 × 4 × 4 = 16` evaluations, normally up to 32 ngspice invocations. Twenty updates allow up to 320 evaluations / 640 invocations, plus validation and baseline runs. With four CPU workers and the local mean, simulation work alone has an ideal lower bound near five seconds per update. Compilation, model generation/backpropagation, slow circuits, queues, and storage add time. Each ngspice invocation has a 30-second timeout.

Before scaling: verify finite loss and changed LoRA weights on approved tasks, restore one checkpoint, measure peak HBM and step time, inspect invalid-action/timeouts/context truncation, and check that group scores vary. Identical scores produce no useful relative reward signal. Compare success rate and simulator calls against the untrained model and seeded random search under identical budgets, then add an established sizing optimizer and multiple seeds. High training reward alone is not evidence of generalization.

Back up completed artifacts with `gcloud storage rsync --recursive runs/EXPERIMENT gs://YOUR_BUCKET/runs/EXPERIMENT`. Also back up `runs/environment/` and any additional task catalogs. Keep full logs during the pilot; simulation artifacts grow with every evaluation. Do not copy credentials into run artifacts.

When finished, print the `--phase cleanup` command on your local machine and delete the VM early instead of waiting for expiry. This leaves the disk and bucket intact for reuse. Track their ongoing storage costs separately.
