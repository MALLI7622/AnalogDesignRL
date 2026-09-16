# Train on the generated tasks, evaluate with AnalogGym

Reviewed against this workspace on **2026-09-15**.

The experiment is to train a model on our paper-informed amplifier-sizing
exercises, then measure whether training improves its performance on separate
AnalogGym problems. Start with the existing **174 training / 21 validation /
55 test** split. Decide the external AnalogGym test set before training.

The 250-task dataset is generated and simulation-verified. Model training and
external AnalogGym evaluation have **not** been performed. This README describes
the experiment and the remaining integration work; it is not a ready-to-launch
training script.

## 1. Understand the experiment

A circuit **topology** is how its components are connected. **Sizing** means
choosing the allowed transistor dimensions, multiplicities, capacitances, and
bias currents. Our tasks keep the connections and requirements fixed while the
model changes permitted values and receives simulator feedback.

Think of the training tasks as practice and the test tasks as the final exam.
The validation set is used during development to choose settings and checkpoints.
The final test set is reserved until those choices are fixed.

Many training exercises give a helpful starting design. Across the full release,
918 of 1,497 editable coordinates match one saved feasible reference exactly;
215 of 250 tasks can reach that reference by changing only one or two controls.
These tasks exercise repair and tuning. A high training score alone does not
establish ability to size an unfamiliar circuit from an arbitrary starting point.
The saved reference is one feasible solution, not necessarily the only solution.

## 2. Find the data

Run commands from the repository root. On this machine that is:

```text
/Users/cheriearjun/Documents/AnalogDesignRL
```

| Path | Purpose |
|---|---|
| [Dataset README](datasets/analog_benchmark_250_v1/README.md) | Dataset contract, source provenance, verification, and difficulty rules |
| [Task catalog](datasets/analog_benchmark_250_v1/TASK_CATALOG.md) | Browse all 250 exercises |
| [tasks/](datasets/analog_benchmark_250_v1/tasks/) | Executable task JSON files for the trusted evaluator |
| [index.json](datasets/analog_benchmark_250_v1/index.json) | Frozen task identities, hashes, difficulties, and splits |
| [worker_catalog.json](datasets/analog_benchmark_250_v1/worker_catalog.json) | Adapter for the existing numeric worker; contains all 250 IDs |
| [Learner export](datasets/analog_benchmark_250_v1_learner/README.md) | Public specifications without private solutions; these are not executable simulator tasks |
| [Private solution catalog](datasets/analog_benchmark_250_v1/private/README.md) | Verified parameter vectors and solution explanations for evaluator-side inspection |
| [Curation notes](datasets/analog_benchmark_250_v1/CURATION_NOTES.md) | Selection, source limitations, and solution-overlap checks |
| [Generation pipeline](benchmark/README.md) | How the 250-task release was constructed |

Keep private solutions, discovery data, calibration traces, and measurement caches
inaccessible to the learner. Export filtering alone does not provide process or
filesystem isolation. Dataset files and raw evidence are local artifacts; copying
the source repository alone may not transfer them to another machine.

Preserve the released index and task files. Its original assembly status is
historical; the final verification status is recorded in
[audit_summary.json](datasets/analog_benchmark_250_v1/audit_summary.json).
Changing the index would invalidate its verification binding.

## 3. Use the existing split for the first experiment

The complete dataset has **100 easy, 125 medium, and 25 hard tasks**. Those counts
describe all three partitions together, not the training partition alone.

| Partition | Easy | Medium | Hard | Total | Use |
|---|---:|---:|---:|---:|---|
| Train | 69 | 87 | 18 | **174** | Update model weights |
| Validation | 6 | 12 | 3 | **21** | Choose hyperparameters and checkpoints; no gradient updates |
| Test | 25 | 26 | 4 | **55** | Final internal evaluation after choices are fixed |
| Total | **100** | **125** | **25** | **250** | Complete release |

The split keeps whole source families together:

| Circuit structure | Tasks | Partition | Implementation source |
|---|---:|---|---|
| Fan SMC | 29 | Train | AnalogGym |
| HoiLee AFFC | 28 | Train | AnalogGym |
| Leung DFCFC1 | 23 | Train | AnalogGym |
| Leung DFCFC2 | 23 | Train | AnalogGym |
| Leung NMCNR | 24 | Train | AnalogGym |
| Peng ACBC | 24 | Train | AnalogGym |
| Song / Guo–Lee DACFC | 23 | Train | AnalogGym |
| Ramos PFC | 21 | Validation | AnalogGym |
| Peng IAC | 26 | Test | AnalogGym |
| AutoCkt two-stage | 29 | Test | Project reconstruction adapted to SKY130 |

All three Leung structures belong to one family. Their related variants should
not be presented as unseen-family tests after training on this family. These are
holdouts from **model training**; all release circuits were studied during dataset
construction, and an untrained foundation model's prior exposure is unknown.

Inspect the saved partition without running simulations or training:

```sh
python3 -B - <<'PY'
import json
from collections import Counter
from pathlib import Path

root = Path("datasets/analog_benchmark_250_v1")
index = json.loads((root / "index.json").read_text())
for split in ("train", "validation", "test"):
    rows = [row for row in index["tasks"] if row["split"] == split]
    print(split, len(rows), dict(Counter(row["difficulty"] for row in rows)))
    print("  circuits:", ", ".join(sorted({row["topology"] for row in rows})))
PY
```

## 4. Account for the overlap with AnalogGym

**221 of the 250 tasks use nine circuit netlists copied from AnalogGym.** The
remaining 29 use the AutoCkt adaptation. Our parameter bounds, starting values,
requirements, and testbenches were developed for this project; the nine imported
netlists are unchanged from the pinned source revision:

```text
Repository: https://github.com/CODA-Team/AnalogGym
Commit:     0a9d1390ade361e2b4a2d33181e22367edbb8afc
Local copy: external/AnalogGym/
```

Therefore, training on these paper-informed tasks does not automatically make
AnalogGym an independent source of test circuits.

| External evaluation | What the result measures |
|---|---|
| AnalogGym Fan after training on our Fan tasks | Transfer to new requirements or test conditions on a familiar structure |
| AnalogGym Peng IAC with the existing split preserved | Transfer to a circuit family excluded from model training |

Report familiar-family and held-out-family scores separately. Changing filenames,
targets, initial values, or a dataset label does not remove shared circuit ancestry.
Different test conditions can still make a useful transfer experiment, but do
not turn the underlying structure into an unseen one.

Peng IAC is a candidate for an external held-out-family experiment under the
current split. Its native AnalogGym evaluation configuration still needs checking;
our successful custom simulations do not validate every upstream testbench.
Ramos is used for validation, so it should not also be claimed as untouched final
test evidence. AutoCkt remains an internal test family.

Other unused local AnalogGym netlists need qualification before selection. Existing
reviews record unresolved simulation or source issues; absence from the training
set does not establish readiness for testing. See the
[source review](reports/benchmark_paper_sources.md).

## 5. Freeze the external evaluation before training

Record the following in an experiment-specific manifest before training and
before evaluating the final test set. Choose checkpoints using validation only:

- Exact AnalogGym revision, circuit IDs, and family assignments; exclude test
  families from training data, demonstrations, and checkpoint selection.
- Parameter controls and bounds, device models, supply, temperature, load,
  testbench files, extraction code, and score definition, with content hashes.
- Initial-design policy and evaluation seeds. Use the same starts for paired
  before/after comparisons. Record invalid and already-passing starts explicitly;
  never count a simulator failure as evidence of a hard circuit.
- Logical simulator-call budget, initial-evaluation convention, timeouts, model
  decoding settings, and fixed handling of invalid actions.
- Checkpoint selection rule using validation only, and the planned metrics.

Use AnalogGym's selected testbench and measurement protocol when reporting an
AnalogGym result. Its repository supplies separate netlists, variables,
testbenches, extraction scripts, and scoring documentation. Pin the version:
the upstream README records a revision to its amplifier score formula.
See the [official repository](https://github.com/CODA-Team/AnalogGym#usage) and
[AnalogGym paper](https://arxiv.org/abs/2409.08534).

Our eight-constraint success score is a separate contract. If the external test
uses a different score or call budget, report that difference and rerun all
comparison methods under the chosen external contract. Do not equate a custom
adapter's score with a published AnalogGym result without matching the setup.

## 6. Compare before and after training fairly

Evaluate the original model and the selected trained checkpoint with the same
observations, starting designs, seeds, and budgets. Include the four existing
controls: uniform linear search, uniform logarithmic search, coordinate search,
and engineering sweep. The latter two already search around the supplied start.
A small-step local-search control can be added with a frozen policy.

For comparisons with the current 250-task calibration, every episode has
**30 logical evaluations**:

1. The trusted driver evaluates the supplied start with `episode.step({})`.
2. It gives the initial feedback and current specification to the learner.
3. The learner has up to **29 remaining evaluations** and stops on success.

Invalid actions and failed simulations consume budget. Simulator-cache reuse
does not give a method extra logical attempts. Matching this convention requires
both episode accounting and client/worker integration, not just a changed number
in a configuration file.

Report success by calls 5, 10, and 30 when using that budget; native AnalogGym
scores where applicable; calls used; and invalid-action/simulation-failure rates.
Show per-family results and variation across seeds. State how failed episodes
are handled when reporting median calls. The original calibration results were
used to curate the dataset; fresh evaluation seeds provide the new comparison.

Keep the fixed starts for a direct v1 comparison. A separate experiment with
starts sampled independently of the saved references can examine start
dependence. Validate and freeze that distribution separately; do not silently
replace v1 starts or carry its difficulty labels onto changed tasks.

## 7. Connect the training implementation

The existing [TPU training starter](training/README.md) contains the infrastructure
instructions. Its current configuration selects Gemma 3 1B-IT with LoRA/GRPO.
That is an implementation starting point, not a measured best model for this data.

The following gaps remain as of the review date:

| Component | Current state | Work needed for this experiment |
|---|---|---|
| Catalog routing | The trainer gets tasks from the HTTP worker; the model config has no catalog path | Explicitly bind the worker to an experiment catalog derived from the release, preserving task hashes and family splits. The starter's smoke catalog is not this dataset |
| Training/test access | Released `worker_catalog.json` contains all 250 tasks; `scripts/prepare_training_bundle.py` stages the 174/21 subset | Use the filtered `train_validation_catalog.json` for training; retain the original release unchanged |
| Attempt limit | `max_episode_steps` is **4** | Choose and document the training horizon; final comparisons against the saved calibration need the full 30-call convention |
| Initial observation | Worker/client construction does not evaluate the start automatically | Implement the charged initial evaluation, returned feedback, step numbering, remaining budget, and reward accounting consistently |
| Circuit information | Worker uses base `Episode`, exposing numeric specifications | Use the same observation contract for all model comparisons. Integrate `BenchmarkEpisode` if symbolic netlists and circuit context are needed; exporting a catalog does not activate it |
| Trained-checkpoint evaluation | Rollout supports `--restore-checkpoint` with `--restore-run`, checking model/checkpoint provenance and final adapter hashes | Validate the real TPU save/reload path before treating restored rollout as an after-training evaluation; training resume remains unsupported |
| Context capacity | Default prompt limit is **3,072 tokens** | Check complete requirements, any circuit context, and longer feedback histories fit; simply increasing attempts is insufficient |
| Training qualification | Existing qualification file has no approvals; a separately authorized bounded research-pilot mode is implemented | Follow the [pilot guide](training/RESEARCH_PILOT_README.md) for that experiment; ordinary training mode still requires real review records |
| Hardware | Ubuntu setup, simulator checks, and JAX detection of four v5e devices passed; actual model training remains unverified | Complete a small authorized run and verify changed weights, checkpoint restore, and matched before/after evaluation before scaling |

A short training horizon can be a deliberate warm-up, but its scores must not be
compared directly with methods given more attempts. Validation selects a
checkpoint; it does not update model weights. Do not let final test results guide
reward changes, prompts, hyperparameters, or checkpoint selection.

The following command only prints the existing configuration's plan. It does
not load model weights, start a worker, create cloud resources, or train:

```sh
python3 -B -m training.train --mode plan --config training/configs/gemma3_1b.json
```

With the current configuration it describes 20 updates, four episodes per update,
and four attempts per episode. Plan mode does not validate a catalog, training
qualification, checkpoint restoration, context fit, or TPU readiness.

## 8. If you choose to train on all 250 later

Treat that as a different experiment with a new training manifest. The 21
validation and 55 test tasks would no longer be held out from training. Their
scores could describe training-set performance but not unseen-task performance.

For an unseen-family AnalogGym claim, select and validate evaluation families
absent from all training examples. For familiar-family transfer, disclose the
shared circuits and report that result separately. Preserve v1 and its historical
verification evidence; create new manifests for changed assignments or starts.

## 9. What is already verified

The release has 250 valid failing starts and 250 reference solutions that pass
all eight requirements at nominal and finer simulation resolution. Its final
check completed 760 uncached circuit evaluations, including ten public defaults,
with zero verification errors. The project suite passed 288 unit tests and all
eight reported check categories. See the
[fresh release report](runs/benchmark_release_verification_001/verification.json)
and [project report](runs/benchmark_final_project_verification_001/verification.json).

These checks establish feasibility under the recorded nominal SKY130 conditions.
They do not measure a trained model's generalization or validate layout, silicon,
noise, mismatch, or process/temperature robustness. Those claims require their
own experiments. The immediate sequence is: fix the external test protocol,
finish the integration gaps above, establish matched baselines, train using only
the designated training partition, select using validation, then evaluate the
frozen checkpoint on the reserved tests.
