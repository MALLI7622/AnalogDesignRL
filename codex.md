# AnalogDesignRL — Codex handoff

Updated: 2026-09-10 (America/New_York). Repository: https://github.com/MALLI7622/AnalogDesignRL

Read this file before continuing in another Codex session. Run repository commands from the project root.

## What we are building

Generate analog-circuit sizing tasks from published designs, validate them with fixed simulations, and measure whether RL improves a model's ability to solve them.

- **Task generator:** GPT-6 Astra (`gpt-6-astra`) through the signed-in Codex CLI. The user has no separate OpenAI API access. Provider adapters also support OpenAI API, Gemini, DeepSeek, Z.AI/GLM, and OpenAI-compatible servers.
- **Trainable solver:** `google/gemma-3-1b-it`, using LoRA and multi-turn GRPO with JAX/Flax and Tunix 0.1.7. Gemma proposes numeric parameter edits; ngspice runs on host CPUs and returns measurements. The TPU runs the model.
- **Research question:** Does training improve success rate and simulations needed, compared with the untrained model and search baselines under equal budgets?

The committed pilot has six AI-generated tasks, two circuit structures, and five requirement groups. Local simulator/HTTP checks have passed. TPU access and remote Codex sign-in now work; the bootstrap, Gemma rollout, gradients, memory use, and checkpoint recovery still need hardware validation. No model training has completed. `verification/qualification.json` currently approves no tasks for training.

## Tasks, sources, and rewards

| Location | Contents |
|---|---|
| [datasets/pilot_20260909/tasks/](datasets/pilot_20260909/tasks/) | Six generated task JSONs; this is the current published task collection, not a database server. |
| [tasks/](tasks/) | Manual sizing fixtures and passing `_nominal.json` installation checks. |
| [circuits/](circuits/) | Netlists, fixed dimensions, reference parameters, and `source.json` provenance. |
| [analog_design/](analog_design/) | Proposal validation, simulation, metric extraction, qualification, and episode API. |
| [generation/README.md](generation/README.md) | Generator providers, source inputs, output format, and rejection handling. |
| [training/](training/) | CPU worker, task catalogs, Gemma trainer, bootstrap, and configuration. |
| [reports/verification.md](reports/verification.md) | Automated verifier evidence and remaining review requirements. |

Fan SMC uses a pinned AnalogGym netlist. The two-stage circuit is a manual reconstruction of AutoCkt Figure 6 adapted from 45 nm devices to SKY130. Its task targets are project/model proposals, not reproduced paper performance numbers. The separate audit of AutoCkt's released 45 nm setup is in `verification/autockt_cases.json`. More sizing variants do not add circuit structures. BAG and physical layout are deferred.

An accepted proposal needs a reference design that passes every requirement and a starting design with valid measurements that fails at least one. The eight requirements cover gain, unity-gain frequency, phase margin, power, DC error, tracking error, and rising/falling settling time. Python extraction must agree with ngspice's native measurements. Each pilot template exposes three adjustable parameters; other transistor widths/lengths remain in the circuit files.

Actions contain absolute numeric values for allowed parameters. Omitted values retain their current settings. An episode permits 30 evaluations, including invalid actions; success ends it early. The current trainer uses a smaller four-attempt cap and tells the model about it.

Reward is `+1` when every constraint passes. Otherwise it is the negative mean of normalized violations, each capped at `1`; invalid actions, failed simulations, timeouts, missing measurements, and measurement disagreement get `-1`. Tunix receives differences between successive scores so its summed episode reward equals the final evaluator score. Passing these tests establishes compliance under the recorded simulation conditions. It does not establish useful learning, unseen-circuit generalization, or production readiness.

Keep the verifier and targets fixed during comparisons. Training requires genuine review records, frozen splits, and controlled learner access to the evaluator. Public nominal fixtures and episode summaries reveal solutions; use them for installation checks. Private references, raw trajectories, and evaluator files must stay outside learner inputs.

## Current TPU and cloud constraints

The successful allocation used the **Cloud TPU queued-resources API**, after v6e requests failed for lack of capacity:

| Setting | Value |
|---|---|
| Programme-linked project | `interpretable-ml-moleculelens` |
| Zone | `us-west4-a` |
| TPU / queued resource | `analog-rl-v5e` / `analog-rl-v5e-request` |
| Accelerator / runtime | `v5litepod-4` / `v2-alpha-tpuv5-lite` |
| Provisioning | FLEX_START, four-hour run limit |

This is a historical allocation record, not a guarantee the TPU is still running. Its recorded termination time was **2026-09-11 06:34 UTC (02:34 Eastern)**. Check current status before continuing. Older provisioning examples in `training/README.md`, `training/cloud_plan.py`, and `project.md` describe a v6e plan; they are not the record of this v5e allocation.

No separate data disk was attached to this v5e TPU. The earlier `analog-rl-data` disk in `us-central1-a` is separate. Save needed files/checkpoints outside the boot disk before deletion or expiry. Queued-resource TPUs do not support stop/start; see [Google's resource-management documentation](https://docs.cloud.google.com/tpu/docs/managing-tpus-tpu-vm). Storage retention and training-checkpoint restore are separate problems; trainer resume is not implemented yet.

**User constraint:** ask before creating, resizing, deleting, or extending billable resources, changing billing, or switching projects. A request to run existing code is not permission to provision more cloud resources. Inspect existing resources before retrying; avoid duplicate allocations. The user prefers the expiring credit, but its SKU eligibility and credit-application order have not been confirmed. Keep account credentials, credit screenshots, and the programme guide out of Git.

Codex sign-in works through normal browser authentication with SSH forwarding. For a new connection, run this **on the Mac**, replacing `YOUR_PROGRAMME_GOOGLE_ACCOUNT` with the already-confirmed Google account:

```bash
gcloud alpha compute tpus tpu-vm ssh cheriearjun@analog-rl-v5e \
  --project=interpretable-ml-moleculelens \
  --account=YOUR_PROGRAMME_GOOGLE_ACCOUNT \
  --zone=us-west4-a \
  -- -L 1455:localhost:1455 -o ExitOnForwardFailure=yes
```

Then run `codex login` on the TPU if needed and open its new link on the Mac. Keep that SSH connection open during login. The tunnel forwards the browser callback to the TPU. Device-code login may require a workspace administrator; this organization account successfully used the normal flow. See [OpenAI's remote-login instructions](https://learn.chatgpt.com/docs/auth#fallback-forward-the-localhost-callback-over-ssh).

## Install and verify

On the **existing Ubuntu TPU**, clone once into the chosen workspace. On the current allocation, a home-directory clone is temporary boot-disk storage. Reuse an existing checkout rather than creating a second one.

```bash
git clone https://github.com/MALLI7622/AnalogDesignRL.git
cd AnalogDesignRL
bash training/bootstrap.sh
```

Bootstrap installs host packages, Python 3.12, pinned TPU dependencies, checksum-verified ngspice 47, and pinned circuit/device-model sources. It runs preflight, verifier, and HTTP smoke checks. Its Ubuntu/TPU execution remains unvalidated: inspect the first failure before retrying or changing dependencies. It does not provision cloud resources or train the model.

In **each TPU shell**, enter the same repository and activate the environment:

```bash
source .venv-tpu/bin/activate
export PATH="$PWD/.deps/ngspice-47/bin:$PATH"
export HF_HOME="$PWD/.cache/huggingface"
export JAX_COMPILATION_CACHE_DIR="$PWD/.cache/jax"
export JAX_COMPILATION_CACHE_MAX_SIZE=21474836480
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
```

These commands also work with the currently committed bootstrap; they do not depend on unpublished activation scripts. Caches live beside the checkout and inherit its storage lifetime.

For CPU-only development, use Python 3.10+ and ngspice 47 (`brew install ngspice` on macOS), then run `python3 scripts/setup.py`. The core simulator/generator uses the Python standard library; TPU dependencies are optional for CPU checks.

Run checks individually and inspect failures:

```bash
python3 -m training.preflight
python3 scripts/verify.py
python3 scripts/evaluate.py --task tasks/autockt_two_stage_nominal.json
python3 -m training.smoke
python3 -m unittest discover -s tests -q
python3 -m training.train --mode plan
```

The nominal fixture should exit `0`; an unmodified failing sizing task normally exits `1`. `verify.py` passing does not grant training approval. `training.smoke` exercises the real HTTP/ngspice loop without an LLM. `--mode plan` prints configuration without downloading weights or running training. Evidence goes under `runs/`.

On the TPU, check JAX after installation:

```bash
python3 -m training.preflight --tpu
python3 -c 'import jax; print(jax.default_backend()); print(jax.devices())'
```

Expect backend `tpu` and four devices for this allocation. `nvidia-smi` is for NVIDIA GPUs. A missing JAX package or CPU-only backend requires checking the Python/TPU runtime installation.

## Generate another task

Check `codex login status` first. Preview a bounded request:

```bash
python3 scripts/generate_tasks.py \
  --config generation/providers/codex.json \
  --template autockt_two_stage \
  --count 1 --max-attempts 3 \
  --request-timeout 600 --simulator-timeout 30 \
  --output datasets/autockt_preview_001 \
  --dry-run
```

Inspect `request_preview.json`. To generate, remove `--dry-run` and use a **new** output directory. This calls Codex and consumes its account allowance. Add `--source /path/to/paper.pdf` for paper text; PDFs require Poppler's `pdftotext` and are not included in a fresh clone. Without that option, the prompt still includes the template's circuit files and provenance. Use `--template fan_smc` for the other supported circuit.

Accepted tasks are in `OUTPUT/tasks/`; references are in `OUTPUT/private/`; requests, replies, rejection reasons, and simulations are in `OUTPUT/attempts/`. Generated batches are ignored by Git by default. Use repeatable `--exclude-batch PREVIOUS_BATCH` arguments to avoid duplicates. Adding a new topology requires a sourced netlist, compatible device models, and verified testbench; the generator does not reconstruct arbitrary paper schematics automatically.

## First Gemma rollout, then training

First obtain access to [Gemma 3 1B-IT](https://huggingface.co/google/gemma-3-1b-it) using the user's Hugging Face account and accept its terms. In the activated environment, use `hf auth login`, or supply `HF_TOKEN` privately. See [Hugging Face CLI authentication](https://huggingface.co/docs/huggingface_hub/guides/cli#hf-auth-login). Never put tokens in this document, shell commands shared in chat, or committed files.

Create a worker token once; reuse it across shells:

```bash
mkdir -p .cache
python3 - <<'PY'
import os
import secrets
from pathlib import Path

path = Path('.cache/worker.token')
if not path.exists():
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(secrets.token_urlsafe(32))
PY
export ANALOG_WORKER_TOKEN="$(cat .cache/worker.token)"
```

In terminal one, start the simulator worker:

```bash
python3 -m training.worker \
  --catalog training/configs/smoke_tasks.json \
  --output runs/evaluation_worker_001 --mode evaluation --workers 4
```

In terminal two, enter the same checkout, repeat environment activation, and run:

```bash
export ANALOG_WORKER_TOKEN="$(cat .cache/worker.token)"
python3 -m training.train --mode rollout --split smoke \
  --output runs/gemma_initial_rollout_001
```

Use new output directories for each run. The worker listens on authenticated `127.0.0.1:8765`; it needs no public firewall rule. Rollout loads Gemma, generates actions, and records attempts in `rollouts.jsonl` without updating weights. This is the next model/runtime check, not yet a controlled research baseline.

Actual RL needs a reviewed catalog with frozen `train` and `validation` splits, valid task/evaluator approvals, and a worker started with `--mode training`. `training.catalog` creates catalogs initially labeled `smoke`; changing labels alone does not approve tasks. Preserve topology groups for structural generalization, or requirement groups for studies within known topologies. See [training instructions](training/README.md#training-and-reward) for catalog construction and checkpoint options.

Only after those prerequisites, the trainer command is:

```bash
python3 -m training.train --mode train --output runs/gemma_experiment_001
```

The default config has four attempts per episode, four samples per task, LoRA rank 16, and 20 updates. Record finite loss, changed adapter weights, HBM use, elapsed time, invalid actions, timeouts, and truncation. Implement and test checkpoint restore before longer runs. Compare with the untrained model and `scripts/random_search.py` under the same conditions and evaluation budget.

## Unfinished local work and next session

This handoff is being published separately from other uncommitted work. A local benchmark expansion targets 250 tasks across ten structures from eight papers, with difficulty measured by search baselines. Its last handoff says screening is unfinished and no final release or training approval exists. If present, read `BENCHMARK_RESUME.md` and `benchmark/WORK_LOG.md` before touching that work; they and the benchmark artifacts are not part of this handoff commit. Never launch duplicate screening processes or modify a cache another process is writing.

Local `training/activate.sh`, `training/startup.sh`, and storage/bootstrap edits also remain unpublished in this commit. Inspect `git status --short` before changing or staging files. Preserve another session's work and keep private references, credentials, downloaded papers, and large run artifacts out of accidental commits.

**Next task:** verify the existing TPU's remaining lifetime and installed environment, run bootstrap/preflight, then attempt the small Gemma rollout. Resolve runtime errors from evidence. Keep the qualification gate intact while task review, benchmark completion, frozen splits, and training recovery are still pending. Use concise updates and ask before additional cloud spending.
