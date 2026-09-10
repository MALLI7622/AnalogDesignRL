# AnalogDesignRL

Use an AI model to generate analog-circuit sizing tasks from published designs, then measure whether RL improves a model's ability to meet fixed requirements. See [the plan](project.md), [current verification results](reports/verification.md), and [initial pilot results](reports/baseline.md).

Pipeline: **papers/design files → AI-generated task candidates → Python/ngspice validation → dataset**. The generator supports your signed-in Codex account, OpenAI API, Gemini, DeepSeek, GLM through Z.AI, and OpenAI-compatible model servers. See [generation setup and commands](generation/README.md). The original pilot tasks were authored manually to develop the verifier.

The pilot contains two amplifier topologies, SKY130 device models, ngspice testbenches, a numeric-action evaluator, and a budgeted episode API. A separate audit runs AutoCkt's released 45 nm netlist and models. Automated verification checks pass; independent review and training approval remain pending. No model has been trained. The [TPU training starter](training/README.md) adds a CPU simulator service and a Gemma 3 1B LoRA/GRPO integration; its TPU runtime still needs hardware validation.

| Circuit | Source | Sizing task |
|---|---|---|
| Fan SMC | Pinned AnalogGym netlist | [fan_smc_sizing.json](tasks/fan_smc_sizing.json) |
| Two-stage Miller amplifier | Manual reconstruction of AutoCkt Figure 6, adapted to SKY130 | [autockt_two_stage_sizing.json](tasks/autockt_two_stage_sizing.json) |

Each sizing task starts from a failing design and permits three parameter edits. Files ending in `_nominal.json` contain passing designs for checking the installation. Source details and assumptions are in each circuit's `source.json`.

Requires Python 3.10+, Git, and ngspice; validated with Python 3.13.7 and ngspice 47 on macOS arm64. The core uses only Python's standard library. From the repository root:

```sh
brew install ngspice  # macOS, if missing
python3 scripts/setup.py
python3 scripts/verify.py
python3 scripts/evaluate.py --task tasks/fan_smc_nominal.json
python3 scripts/evaluate.py --task tasks/autockt_two_stage_nominal.json
python3 -m unittest discover -s tests -v
```

Setup downloads pinned AnalogGym and AutoCkt revisions and checks their model/source hashes against [dependencies.lock.json](dependencies.lock.json). Dependencies remain in ignored `external/` and `.deps/` directories.

Generate tasks through Codex after signing in with ChatGPT using `codex login`:

```sh
python3 scripts/generate_tasks.py --config generation/providers/codex.json --count 1
```

Accepted tasks go to `datasets/generated_*/tasks/`; reference answers and verification logs are stored alongside them in separate directories. Codex mode uses your existing login without an API key. Direct API providers remain available with their own credentials. Use `--source paper.pdf` to include paper text, `--dry-run` to preview the request, or `--replay generation/examples/autockt_reply.json` without provider options for an offline pipeline check. Generation currently uses the two supported circuit templates; it does not reconstruct arbitrary new schematics automatically.

The first batch contains [six generated tasks](datasets/pilot_20260909/README.md), with [simulation and episode checks](reports/pilot_20260909.md). Their JSON files are together in `datasets/pilot_20260909/tasks/`.

`scripts/verify.py` runs analytical AC/settling checks, source reproduction, numerical refinement, deliberately failing circuits, and pilot regressions. It saves a JSON report and raw simulator data under `runs/verification_*/`. Exit code `0` means the automated checks passed; training approval is a separate field. The 45 nm audit cases are in [verification/autockt_cases.json](verification/autockt_cases.json).

Evaluate an edited design by passing `--parameters candidate.json`, containing a JSON object of allowed numeric values. Every run saves the task, parameters, testbenches, simulator logs, measurements, commands, and provenance hashes under `runs/`. Exit code `0` means the design passed; `1` means it did not.

Run a reproducible random-search comparison:

```sh
python3 scripts/random_search.py --task tasks/fan_smc_sizing.json --seed 0
python3 scripts/random_search.py --task tasks/autockt_two_stage_sizing.json --seed 0
```

For evaluation and pipeline checks, use the same loop:

```python
from analog_design.episode import Episode

episode = Episode("tasks/fan_smc_sizing.json", "runs/my_episode")
specification = episode.specification()
feedback = episode.step({})  # Evaluate the initial design; counts toward the budget.
feedback = episode.step({"CAPACITOR_0": 5e-12})  # Absolute value; other values persist.
```

Each episode allows 30 evaluations and ends on success or budget exhaustion. Invalid actions consume an evaluation. A valid evaluation uses up to two simulator invocations, each limited to 30 seconds. Call `step` only while `episode.done` is false; output directories must be new.

Success requires every constraint to pass and agreement between Python extraction and ngspice's native measurements. Reward is `1` on success; otherwise it is the negative mean of normalized constraint violations, each capped at `1`. Invalid parameters, failed simulations, timeouts, incomplete measurements, or measurement disagreement receive `-1`. Disagreement is unresolved verification, not proof that a circuit is physically bad. Completed measurements can have `status: ok` while `success: false`.

Training integrations must construct `Episode(..., for_training=True)`. This requires a current task/evaluator review record in [verification/qualification.json](verification/qualification.json); no tasks are approved yet. The default mode remains available for evaluation and debugging.

The [training worker](training/README.md) exposes numeric actions and simulator feedback over an authenticated loopback API, without reference files or executable model tools. The trusted trainer process is not placed in an OS sandbox. Training qualification, deployment isolation review, held-out tasks, and TPU validation remain pending. The public reference tasks are installation checks.
