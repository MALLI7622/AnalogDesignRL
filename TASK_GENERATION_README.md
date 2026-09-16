# Creating more analog RL tasks

Handoff for another Codex session. Checked against the repository on 2026-09-10. Run commands from the repository root.

The goal is to generate circuit-sizing problems with AI, verify them with ngspice, and later measure whether RL improves design ability. Task generation runs on the local CPU and uses the existing Codex login. It does not need the TPU.

## Completed 250-task benchmark

The [training and AnalogGym evaluation setup](TRAINING_ANALOGGYM_README.md) explains how to use this release for the next experiment while keeping circuit families out of training for evaluation.

The [250-task benchmark](datasets/analog_benchmark_250_v1/README.md) is complete as of 2026-09-11: **100 easy, 125 medium, and 25 hard**, covering ten amplifier structures from eight papers. Every task has a distinct requirement group and a [verified reference solution](datasets/analog_benchmark_250_v1/private/README.md). All 250 starts, reference solutions, and refined-resolution reference solutions passed their expected fresh checks; all ten public defaults failed the corresponding tasks. The full project suite passed 288 unit tests and all simulator regression checks.

Use the [task catalog](datasets/analog_benchmark_250_v1/TASK_CATALOG.md) to browse the problems and the [learner export](datasets/analog_benchmark_250_v1_learner/README.md) for public specifications. Keep reference solutions and calibration evidence in the trusted evaluator. Splits hold out whole circuit families: 174 train, 21 validation, and 55 test tasks.

Difficulty is measured relative to four search methods with 30 evaluations, not an untested LLM. The tasks use nominal SKY130 circuit adaptations and project-derived targets. [Curation notes](datasets/analog_benchmark_250_v1/CURATION_NOTES.md) document source limitations, solution overlap, and comparisons with all seven older tasks. Model training has not been performed, and the existing qualification gate is unchanged. [BENCHMARK_RESUME.md](BENCHMARK_RESUME.md) records the completed artifacts and retained evidence. The sections below preserve the earlier pilot-generation workflow.

## What we created from the papers

| Template | Where the circuit came from | What we changed |
|---|---|---|
| `autockt_two_stage` | AutoCkt Figure 6, manually transcribed into a netlist | Replaced the paper's 45 nm devices with SKY130 models; selected sizes and test conditions for this project. |
| `fan_smc` | Fan SMC circuit from a pinned AnalogGym revision | Kept the published netlist; added our measurement testbenches and sizing-task requirements. |

Read the exact provenance and assumptions in [AutoCkt source notes](circuits/autockt_two_stage/source.json) and [Fan SMC source notes](circuits/fan_smc/source.json).

**The current task targets are project/model proposals, not recovered paper performance numbers.** The separate verifier audit of AutoCkt's released 45 nm netlist does not make the SKY130 adaptation a reproduction of that paper.

The [six-task pilot](datasets/pilot_20260909/README.md) used `gpt-6-astra` through Codex. AutoCkt requests included extracted paper text; Fan SMC requests used the released circuit files and source notes. The accepted proposals cited circuit files, rather than passages from the paper. All six reused existing passing seed designs. There is also one earlier accepted task in `datasets/codex_verified_test/` on this machine.

Six tasks cover **two circuit structures and five requirement groups**. More sizes or starting points do not add circuit structures.

## How a task is generated and checked

1. Load a fixed circuit template, its netlist, source notes, parameter values, and any supplied paper text.
2. Give the model these inputs and a known passing seed. Request JSON containing starting parameters, reference parameters, targets, source evidence, and assumptions.
3. Validate allowed numeric ranges, integer multiplicities, and exact source quotes. The model can keep or tighten targets; it cannot change connections, device models, test conditions, measurement rules, rewards, or the evaluation budget.
4. Simulate the proposed reference. It must pass every requirement. Simulate the start: it must produce valid measurements and fail at least one requirement. Both evaluations require agreement between Python measurement extraction and ngspice's native measurements.
5. Save accepted tasks and their evidence. Reject malformed proposals, failed simulations, unsupported quotes, and duplicate starts/requirements. Return rejection feedback for another proposal within the attempt limit.

The eight requirements cover gain, unity-gain frequency, phase margin, power, DC error, tracking error, and rising/falling settling time. Each current template exposes three adjustable parameters; remaining device dimensions are fixed in the circuit files.

For example, [this AutoCkt task](datasets/pilot_20260909/tasks/autockt_two_stage_ai_be3ba7a2a09ba7b53a12.json) starts at about **0.483 mW** against a **0.450 mW maximum**. The learner must reduce power while keeping the other seven requirements satisfied. A separately stored reference demonstrates that this is possible under the recorded simulation conditions.

The evaluator gives `+1` when all requirements pass; otherwise it returns the negative mean of normalized violations, each capped at `1`. Invalid evaluations receive `-1`. An episode permits 30 evaluations. This reward measures compliance with the specified tests; it does not establish task difficulty or useful learning.

## Generate a small batch using Codex

This workspace already has the paper and simulation dependencies. On a fresh machine, follow [repository setup](README.md) first. PDF input additionally needs Poppler's `pdftotext` (`brew install poppler` on macOS if missing).

The provider configuration is [generation/providers/codex.json](generation/providers/codex.json). It selects `gpt-6-astra` and the saved ChatGPT login. Check `codex login status`; use `codex login` only if sign-in is needed. Generation consumes that account's usage allowance.

Preview a request without calling a model or simulator:

```sh
python3 scripts/generate_tasks.py \
  --config generation/providers/codex.json \
  --template autockt_two_stage \
  --source external/AutoCkt-2001.01808v2.pdf \
  --exclude-batch datasets/codex_verified_test \
  --exclude-batch datasets/pilot_20260909/autockt_two_stage \
  --exclude-batch datasets/pilot_20260909/autockt_two_stage_retry \
  --count 3 --max-attempts 9 \
  --request-timeout 600 --simulator-timeout 30 \
  --output datasets/autockt_next_preview \
  --dry-run
```

Inspect `request_preview.json`. To generate, run that command with `--dry-run` removed and change the output to a **new** directory, such as `datasets/autockt_next_001`.

For Fan SMC, use this command; its circuit sources are included automatically:

```sh
python3 scripts/generate_tasks.py \
  --config generation/providers/codex.json \
  --template fan_smc \
  --exclude-batch datasets/pilot_20260909/fan_smc \
  --exclude-batch datasets/pilot_20260909/fan_smc_retry \
  --count 3 --max-attempts 9 \
  --request-timeout 600 --simulator-timeout 30 \
  --output datasets/fan_smc_next_001
```

Each proposal uses at most four ngspice invocations. A batch may accept fewer than requested. Keep the partial results; report the failure before increasing budgets or changing providers. To continue an approved batch, use a new output directory, request only the missing count, and also exclude the partial batch.

`--exclude-batch` requires a directory containing `manifest.json` for the **same template**. Add every later batch for that template to future commands. The combined `datasets/pilot_20260909/` directory has an `index.json`, not a batch manifest: do not pass that parent directory as an exclusion.

The original batch directories are local and ignored by Git. If working from a clone, recover their manifests or add support for exclusions from the tracked pilot index before using these commands; do not silently drop duplicate checking.

## Where to inspect results

| Location | Contents |
|---|---|
| `datasets/pilot_20260909/tasks/` | Six existing task JSON files, tracked in Git |
| `datasets/pilot_20260909/index.json` | Pilot inventory, hashes, requirement groups, and measurements |
| `datasets/<new_batch>/tasks/` | New accepted task JSON files |
| `datasets/<new_batch>/private/<task_id>/` | Reference parameters and proposal provenance |
| `datasets/<new_batch>/attempts/` | Requests, replies, rejection reasons, and simulation evidence |
| `datasets/<new_batch>/sources/`, `manifest.json` | Supplied text, hashes, model settings, and batch outcome |

Recheck a start with `python3 scripts/evaluate.py --task PATH_TO_TASK.json`. Exit code `1` is expected for a failing start, but inspect the result: it must say `status: ok` and `success: false`. A simulator error is not an acceptable starting design. Add `--parameters PATH_TO_REFERENCE.json` to check its private reference; that should pass.

## Add a circuit from another paper

Passing a new PDF to `--source` only adds text to an existing template. It does not create a new circuit. The PDF reader does not interpret schematic images.

1. Choose a paper with a usable schematic, device models, and preferably released netlists. Record the exact paper/version, figure, repository revision, and operating conditions. Mark missing values explicitly.
2. Add `circuits/<name>/netlist.spice`, `reference.params`, and `source.json`. Check connectivity, pin order, dimensions, units, biasing, and device models against the source. Record every inferred value or process substitution as an adaptation.
3. Establish a passing reference and a valid failing start. Add `tasks/<name>_nominal.json` and `tasks/<name>_sizing.json` with justified parameter bounds and measurable requirements. The present testbenches support the amplifier interface used by the existing templates; other circuit types need suitable measurement support first.
4. Verify measurement extraction, numerical stability, and deliberately failing designs. Compare against paper results only where models and test conditions match. Run `python3 scripts/verify.py` and relevant tests after implementation changes.
5. Register the template in `TEMPLATES` in [analog_design/task_proposals.py](analog_design/task_proposals.py), then generate a bounded pilot and inspect the evidence before scaling it.

## Checks before calling the dataset ready

Simulation acceptance establishes a feasible sizing exercise under the recorded conditions. Quote matching only establishes that text exists. Neither check establishes correct paper interpretation, fabrication readiness, or generalization to new circuits.

Measure task difficulty against simple baselines and an untrained model before expanding the dataset. Report seed-reference reuse, distinct requirement groups, and circuit count. Keep identical `requirement_group` values together when splitting; close variants also need review, and claims about new topologies require topology-held-out evaluation.

Keep private answers and diagnostic episode solutions away from the learner. Folder names alone do not enforce isolation. Training must use `Episode(..., for_training=True)`; [verification/qualification.json](verification/qualification.json) currently approves no tasks. Do not bypass that gate or invent review records to start training.

## Prompt for the next Codex session

```text
Read TASK_GENERATION_README.md and generation/README.md. Create up to three
additional verified sizing tasks per existing circuit template, using the
saved Codex login and the documented attempt limits. Inspect the source
provenance, preview requests, and exclude all previous matching batches.
Keep the circuit templates and verifier fixed. Save new batches separately
and report accepted/rejected counts, initial failures, reference results,
requirement groups, and seed reuse. Keep tasks unapproved for training.
Use local CPU simulation. Leave training/ and cloud resources untouched;
contact me if a failure requires more spending or a different setup.
Preserve other sessions' edits and do not commit or push unrelated files.
```

For a new paper's circuit, replace the batch request with step 1 of “Add a circuit from another paper” and identify the paper. Full provider options and replay examples are in [generation/README.md](generation/README.md).
