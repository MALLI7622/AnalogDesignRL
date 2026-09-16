# Bounded TPU research pilot

This implementation runs a two-update Gemma 3 1B LoRA/GRPO experiment while
independent analog-engineer review remains pending. It keeps the ordinary
`--mode training` worker and `--mode train` trainer qualification gate intact.
The separate `research-pilot` mode requires an explicitly authorized, expiring
manifest. Preparing or fetching the code grants no authorization.

Status on 2026-09-16: Ubuntu bootstrap, ngspice checks, HTTP smoke tests, and
JAX detection of four v5e devices passed on the existing TPU VM. Gemma weights
were downloaded at the revision pinned in the pilot config. No model rollout,
optimizer update, or real checkpoint roundtrip has completed. The pilot code's
unit tests pass; actual model execution is the next validation step.

## Files and data

- [research_pilot.py](research_pilot.py): draft generation, exact file/hash
  bindings, expiry, and persistent worker usage limits.
- [gemma3_1b_pilot.json](configs/gemma3_1b_pilot.json): two updates, four generated
  episodes per update, and four attempts per episode. Validation uses the first
  three sorted validation IDs, recorded in `run.json`.
- [worker.py](worker.py), [tunix_env.py](tunix_env.py), and [train.py](train.py):
  matching worker/trainer modes and checkpoint provenance/readback.
- [prepare_training_bundle.py](../scripts/prepare_training_bundle.py): stages
  174 training and 21 validation tasks, retaining their bytes and hashes.
  The 55 test tasks and private solutions are excluded.

The data is separate from Git. The previously staged VM paths are:

```text
/home/cheriearjun/AnalogDesignRL/datasets/analog_benchmark_250_v1/tasks/
/home/cheriearjun/AnalogDesignRL/datasets/analog_benchmark_250_v1/train_validation_catalog.json
/home/cheriearjun/AnalogDesignRL/datasets/analog_benchmark_250_v1/training_bundle_manifest.json
```

The local transfer overlay was prepared at
`/Users/cheriearjun/Documents/AnalogDesignRL/runs/training_bundle_20260916_001/`.
Fetching the branch does not download the dataset. The bundle manifest records
transfer provenance; it is not an independent expert approval.

## Check the existing VM

Run from the repository root, after fetching the implementation. These commands
use the existing VM and environment; they do not provision resources.

```bash
source training/activate.sh
test -f datasets/analog_benchmark_250_v1/train_validation_catalog.json
python -m training.preflight
python -m training.preflight --tpu
python -m training.train --mode plan --config training/configs/gemma3_1b_pilot.json
```

The plan permits 8 training episodes and 12 validation episodes, with at most
80 total evaluations. Tunix evaluates at step zero; validation is included in
this limit. Baseline and restored-checkpoint rollouts are separate evaluations.
These four-attempt numeric episodes are an integration experiment, not a matched
comparison against the benchmark's 30-evaluation baselines.

Create the loopback worker credential once if it does not already exist:

```bash
python -c 'import os,secrets; from pathlib import Path; p=Path(".cache/worker.token"); p.parent.mkdir(exist_ok=True); p.exists() or p.write_text(secrets.token_urlsafe(32)); os.chmod(p,0o600)'
source training/activate.sh
```

Keep the token and saved Hugging Face login private. Activation sets `umask 077`
and loads this worker token for each terminal.

## First run one untrained rollout

In terminal one:

```bash
source training/activate.sh
python -m training.worker \
  --catalog datasets/analog_benchmark_250_v1/train_validation_catalog.json \
  --output runs/pilot_baseline_worker_001 --mode evaluation --workers 4
```

In terminal two:

```bash
source training/activate.sh
python -m training.train --mode rollout --split train --max-tasks 1 \
  --config training/configs/gemma3_1b_pilot.json \
  --output runs/pilot_baseline_001
```

Inspect `runs/pilot_baseline_001/rollouts.jsonl` for actions and simulator
feedback. Stop that evaluation worker with Ctrl-C before starting the pilot
worker on the same port. Use fresh output names if a run directory exists.

## Prepare and authorize the pilot

Create the draft after any code/config changes and the baseline check:

```bash
python -m training.research_pilot \
  --catalog datasets/analog_benchmark_250_v1/train_validation_catalog.json \
  --config training/configs/gemma3_1b_pilot.json \
  --evidence datasets/analog_benchmark_250_v1/training_bundle_manifest.json \
  --output runs/research_pilot_001/authorization.json \
  --max-episodes 20 --max-evaluations-per-episode 4 \
  --max-total-evaluations 80 --expires-hours 4
```

This writes **`authorized: false`**. Review the manifest's config, task hashes,
limits, and expiry. Only after the experiment owner explicitly authorizes this
bounded research run should the manifest record:

- `authorized`: `true`.
- `authorization.authorized_by`: the actual person authorizing the experiment.
- `authorization.authorized_utc`: the actual timezone-aware authorization time,
  obtainable with `date -u +%Y-%m-%dT%H:%M:%SZ`.
- `authorization.basis`: the actual request approving these two updates and
  20 episodes / 80 evaluations with independent expert review still pending.

Keep `qualified_training_approved: false` and
`independent_expert_review: "pending"`. Research authorization is not a circuit
review record; do not populate `verification/qualification.json` with invented
reviewer details. No pilot authorization was recorded during preparation.

Changes to bound code, configs, task files, or circuits invalidate the manifest.
Each authorization permits one worker invocation. Usage remains recorded under
`runs/research_pilot_usage/`; restarting a worker cannot reset its allowance.

## Launch the authorized run

In terminal one, once port 8765 is free:

```bash
source training/activate.sh
python -m training.worker \
  --catalog datasets/analog_benchmark_250_v1/train_validation_catalog.json \
  --mode research-pilot --pilot-manifest runs/research_pilot_001/authorization.json \
  --output runs/research_pilot_001/worker --workers 4
```

In terminal two:

```bash
source training/activate.sh
python -m training.train --mode research-pilot \
  --config training/configs/gemma3_1b_pilot.json \
  --pilot-manifest runs/research_pilot_001/authorization.json \
  --output runs/research_pilot_001/train
```

The trainer rejects an evaluation worker for this mode. It records selected
tasks, the immutable model revision, authorization status, and the execution
plan in `train/run.json`. A successful `train/training_result.json` requires
positive optimizer steps, finite changed LoRA weights, and checkpoint readback
with a matching adapter hash. Inspect the metrics for finite loss and useful
reward variation before proposing a larger run.

## Reload the trained checkpoint

Stop the pilot worker and start an evaluation worker using the same catalog and
a new output directory. Then run:

```bash
python -m training.train --mode rollout --split train --max-tasks 1 \
  --config training/configs/gemma3_1b_pilot.json \
  --restore-checkpoint runs/research_pilot_001/train/checkpoints \
  --restore-run runs/research_pilot_001/train/run.json \
  --output runs/research_pilot_001/restored_rollout
```

Checkpoint loading is implemented for rollout; optimizer/session resume remains
unsupported. Restore checks the recorded base model, LoRA config, checkpoint
metadata, and final adapter hash. It currently expects the original checkpoint
root path. Preserve the full run directory outside the VM's temporary boot disk
before the VM expires. This implementation does not extend the VM lifetime.
