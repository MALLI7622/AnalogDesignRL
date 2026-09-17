# Bounded TPU research pilot

This implementation runs a two-update Gemma 3 1B LoRA/GRPO experiment while
independent analog-engineer review remains pending. It keeps the ordinary
`--mode training` worker and `--mode train` trainer qualification gate intact.
The separate `research-pilot` mode requires an explicitly authorized, expiring
manifest. Preparing or fetching the code grants no authorization.

## Larger prompt comparison: 2026-09-17 (completed)

The user requested a larger test of an exploration-focused prompt. The bounded
evaluation is `runs/prompt_comparison_001/`: all 21 validation tasks, four episodes
per task per arm, four attempts per episode, at most 168 episodes / 672 evaluations.
Both arms restore the verified two-update checkpoint from `research_pilot_007`.
All validation tasks are Ramos PFC; this does not expand topology coverage.

The `current` arm preserves the previous prompt. `exploration_v1` removes the
repeated current-values answer example and asks for bounded parameter changes,
feedback-guided adjustments, and avoidance of repeated designs. It changes both
system and observation prompts. These options currently support rollout only;
no training is running. Seeds are paired by task, episode, and within-episode
call, so early success cannot shift subsequent episodes' seeds.

The experiment completed with 168 attempted episodes and 671 evaluation attempts.
The exploration arm overflowed its context after 62 full episodes and three
attempts in episode 63. A continuation skipped those 63 pairs and completed the
remaining 21; the failed episode remains in the denominator. No episodes were
replayed. Prompt/context limits and per-episode seeds stayed fixed. Rollout now
records context errors per episode and supports explicitly skipping attempted
pairs with paired seeds; this is not optimizer resume.

| Metric | Current prompt | Exploration prompt |
|---|---:|---:|
| Solved episodes | 0/84 | 0/84 |
| Accepted actions | 327/336 | 0/335 |
| Valid parameter-changing actions | 0 | 0 |
| Mean final observed reward | -0.106143 | -1.000000 |
| Context-overflow episodes | 0 | 1 |

Every exploration reply had Markdown fences and was rejected as an invalid JSON
action. None reached simulation. The combined change harmed formatting and did
not establish exploration. Keep the current prompt as the default. The next
controlled prompt experiment should preserve its JSON scaffold and change only
the instruction to perturb one allowed value. Do not train on this failed prompt
variant. Since two prompt components changed together, this result does not
identify which component caused the regression.

The plan, source snapshots, logs, and trajectories are retained in the run
directory. `results.json` and `README.md` contain the paired comparison; the
summary validates task/config/model matching and all 335 shared seed contexts.
The worker was stopped after completion. The evidence archive is
`runs/prompt_comparison_001_results.tar.gz`. These artifacts remain local to the
expiring VM; no external backup was created.

## Successful training and matched evaluation: 2026-09-17

`runs/research_pilot_007/` completed two optimizer updates on the existing four
v5e devices. LoRA weights changed, all logged scalar metrics were finite, and
checkpoint 2 passed hash-verified readback and a separate restored rollout.
Peak live memory was 6.97 GiB per device (reserved memory peaked at 11.36 GiB).

The fix splits each four-sequence GRPO group after advantages are computed and
accumulates four sequence gradients for one optimizer update. The configuration
is `training/configs/gemma3_1b_microbatch_pilot.json`. It retains the original
context limits and fixed circuit reward. Tunix's prompt-group microbatch setting
alone does not split the four generated sequences. A post-run guard now rejects
empty response masks to preserve loss normalization; all 16 pilot trajectories
had nonempty masks. Executed code is preserved under the run's `source_snapshot/`.

Matched validation used two unchanged validation tasks, four episodes per task,
four attempts per episode, identical sampling settings and seed sequences:

| Metric | Original | Trained |
|---|---:|---:|
| Solved episodes | 0/8 | 0/8 |
| Mean final reward | -0.275474 | -0.157575 |
| Failed actions | 7/32 | 2/32 |
| Actions changing starting parameters | 0/32 | 0/32 |

The observed improvement is fewer failed actions, not useful sizing exploration.
Two updates on two training tasks and evaluation on two validation tasks do not
establish generalization. The second update had zero policy-gradient advantage.
Before scaling training, address copying of starting values and demonstrate
valid parameter exploration with useful reward variation.

The new user-requested pilot used 16 episodes / 64 evaluations. Its matched
comparison used another 16 episodes / 64 evaluations. Earlier attempts retain
their separate consumed manifests and 16 episodes / 60 evaluations. Results and
raw evidence are indexed in `runs/research_pilot_007/README.md`; the final adapter
is in `runs/research_pilot_007/train/checkpoints/actor/2/`. These are local files,
not an external backup. The existing TPU termination is 2026-09-17 06:53 UTC.

## Earlier failed training attempt: 2026-09-17

The user-authorized diagnostic reached the first gradient update but failed with
TPU HBM exhaustion: `jit__train_step` required 34.64 GiB of temporary memory versus
15.75 GiB available per device. Zero optimizer updates completed; the step-zero
checkpoint is not a trained model. Resolve gradient-memory use before retrying
this configuration on the existing v5e devices.

The attempts are retained under `runs/research_pilot_004/`, `_005/`, and `_006/`.
Together they used 16 episodes and 60 evaluations, below the cumulative authorized
20/80 limits. All processes were stopped. The final retry used two validation
tasks to preserve the remaining budget. The readable trajectory index is
`runs/research_pilot_review/README.md`; raw prompts/replies are in each run's
`train/generations.jsonl`, episode events in `train/trajectories/`, and simulator
evidence in `worker/<episode_id>/`.

Runtime fixes normalize Tunix's ndarray task IDs, expose the `prompts` observation
field needed by GRPO reward processing, and handle string prompts in the seed
logger. The pilot trajectory token limit is now 4,096: the earlier 2,048-token
limit truncated episodes after three actions. The installed-Tunix adapter and
regression suite passed 54 tests. The final retry completed four-step collection
without context truncation but did not demonstrate circuit exploration: actions
repeated the start, with occasional invalid parameter names.

Next engineering work is to split the GRPO group's gradient computation into
smaller sequence microbatches while preserving group advantages and gradient
accumulation, and profile context padding. This run observed a maximum prompt of
3,433 tokens and trajectory of 2,387 tokens; these are observations on three tasks,
not safe bounds for the complete catalog. Preserve the fixed circuit reward.
Do not reuse consumed authorization manifests; saved attempts and cumulative
usage must remain part of any subsequent experiment's provenance.

## Earlier rollout checks

Status on 2026-09-17: an untrained four-attempt rollout completed on the existing
four-device TPU with the pinned Gemma revision. All four actions were valid JSON
and reached ngspice (eight simulator invocations). The model repeated the initial
values and did not solve the task. This establishes formatting and runtime
integration for one task, not useful learning. Evidence is in
`runs/pilot_format_rollout_003/summary.json` and `rollouts.jsonl`; 44 relevant
regression tests passed. No optimizer update or real checkpoint roundtrip has
completed.

Runtime fixes replicate the single KV-head activations across tensor-parallel
devices and give the sampler an integer seed instead of a donated JAX key.
Rollout and training share a prompt showing the already-public current values
in the required flat JSON format. Markdown-wrapped actions remain invalid and
consume budget; feedback now explains format errors. The pilot prompt window
is 6,144 tokens because 3,072 overflowed before the fourth attempt. Regenerate
pilot manifests after these changes; earlier drafts have stale hashes.

Generation now assigns a distinct integer seed to each sampler call, including
calls made by the GRPO learner. `generation_seeds.jsonl` records the call index,
seed, and prompt hashes without copying prompt contents. The sequence is
reproducible for the same base seed, call order, and batching; asynchronous
scheduling can change which prompt receives which seed. Distinct seeds do not
guarantee distinct actions or rewards. Shared sampler calls are serialized.

The four-episode seed diagnostic completed in `runs/pilot_seed_rollout_001/`:
16 distinct seeds, 15 simulated actions (30 ngspice invocations), and one invalid
parameter-name typo. All accepted actions repeated the start; no episode solved
the task. Final rewards differed only because of that typo, so useful sizing
exploration is still unproven. Initial prompts matched across all four episodes,
and 49 seed/training/pilot/bundle regression tests passed. No optimizer ran.

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

For a bounded diversity diagnostic, add `--episodes-per-task 4` and use a new
output directory. This creates four independent episodes from the same original
start, with up to 16 total evaluations for one task. The `episode` field in
`rollouts.jsonl` distinguishes them. Compare final rewards across episodes;
four edits in a single episode are not a GRPO sample group. This rollout check
does not execute optimizer updates or exercise the GRPO gradient path.

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

## Readable trajectory review

The larger prompt comparison has an offline searchable viewer at
`runs/prompt_comparison_001/review/viewer.html` and a Markdown index at
`runs/prompt_comparison_001/review/README.md`. All 671 displayed replies were
checked against raw generation logs; all 168 episodes are included. Each step
shows the exact prompt, reply, and evaluator feedback. Use the matching-episode
button to switch prompts. The portable bundle is
`runs/prompt_comparison_001_review.tar.gz`.
