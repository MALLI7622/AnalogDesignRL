# AnalogDesignRL — Project Plan

Updated: 2026-09-10. Status: first six tasks generated through Codex; simulation and episode checks pass. [TPU training infrastructure](training/README.md) is implemented with local CPU integration checks; TPU execution remains untested. Independent review and training approval remain pending. No model training completed. See [the task batch](reports/pilot_20260909.md), [setup](README.md), and the [verification report](reports/verification.md).

Use an AI model to generate executable circuit-design tasks from published analog circuits, then train and evaluate a model on those tasks. Measure success rate and simulations needed to meet requirements.

The approach draws on three projects:

| Reference | Role in our plan |
|---|---|
| [AMSNet](https://arxiv.org/html/2405.09045) | Reconstruct components and connections from published schematics. |
| [AnalogGym](https://github.com/CODA-Team/AnalogGym) | Package circuits with parameters, testbenches, and performance evaluation. |
| [AutoCkt](https://arxiv.org/abs/2001.01808) | Learn parameter adjustments through simulator feedback. |

Start with amplifier sizing: keep circuit connections fixed and adjust permitted transistor sizes, capacitor values, or bias parameters. Use ngspice, compatible device models, and a Python environment. BAG and physical layout are deferred.

Generation flow: **papers and design files → AI task proposals → Python validation and ngspice checks → dataset**. The AI uses source text to propose sizing values, targets, and reference solutions for a supported circuit. Proposals include source excerpts and assumptions; numeric proposals are labeled as project-generated values. Accept a task only after a reference passes the fixed tests and its starting design produces valid measurements while failing at least one requirement. The generator cannot change verifier code or reward rules. The manually authored pilot tasks supply templates and seed references.

Milestones:

1. **Working: AnalogGym amplifier.** Fan_SMC_Pin_3 runs with its published parameters, pinned SKY130 models, and project-authored tests. Provenance and results are saved. A full independent schematic-to-netlist audit remains pending.
2. **Working: paper reconstruction.** AutoCkt Figure 6 was manually transcribed and adapted from 45 nm predictive devices to SKY130. Project-selected sizes pass the pilot tests; this does not reproduce the paper's numerical results.
3. **Working: common task interface.** Both circuits have bounded numeric edits, fixed requirements, failure handling, logs, and 30-evaluation episodes. Model access must be limited to the evaluator API during training.
4. **Automated checks passing; review pending.** AutoCkt's released netlist runs with its original 45 nm model. The audit includes analytical references, a separately sized passing fixture, known failures, numerical refinement, and two measurement methods. It reproduces the released setup; the paper's aggregate training results have not been reproduced. An analog engineer must review circuit/testbench semantics and model limitations before training approval.
5. **Working: first Codex task batch; difficulty assessment and splits pending.** GPT-6 generated [six tasks](datasets/pilot_20260909/README.md) across two circuits and five requirement groups. Every task passed reference, starting-design, and episode-reward checks. AutoCkt prompts included paper text; accepted quotes cite the circuit files. Interrupted batches can be continued in a new directory with prior tasks excluded. Direct API adapters remain covered by mocked HTTP tests. Keep reference solutions outside learner inputs and freeze evaluation tasks before training.
6. **Partial: baseline performance.** Seeded random search is implemented. Add the untrained model and an established optimizer under matching conditions and budgets.
7. **Infrastructure started: train and evaluate.** Use Gemma 3 1B-IT with LoRA and Tunix GRPO on a TPU VM; run ngspice on its host CPUs through the numeric-action API. Local HTTP/simulator checks pass. Validate model loading, rollouts, gradients, memory use, and checkpoint restore on the allocated TPU before scaling. Actual training still requires approved tasks and frozen splits. Expand circuit structures after the pilot works.

Each task package must contain:

- Source paper and figure, circuit identifier, and reconstruction assumptions.
- Generation model, prompt, source citations, proposed values, and validation results.
- Netlist, device-model references, initial parameters, allowed ranges, and reference solution.
- Requirements and fixed tests, including supply voltage, temperature, and output load.
- Metric extraction, reward rules, success checks, and failure handling.
- Reproduction commands and a log of edits, measurements, rewards, and runtime.

Success requires all specified constraints to pass. Invalid designs, simulation failures, timeouts, and missing measurements cannot count as success. Gain, speed, power, and stability checks should be defined before training; reward rules remain fixed during evaluation.

Before training, require an audited circuit and measurement pipeline, a verified feasible reference, and explicit validation scope. Match the source's models and operating conditions for reproduction; preserve fixed reference values and investigate discrepancies. Label adaptations separately. Extend operating-condition and mismatch coverage where supported and required. Success requires all constraints and agreement between measurement methods. Record approvals in `verification/qualification.json`; training-mode episodes reject unreviewed or stale task/evaluator versions. Enforced agent/evaluator isolation and frozen evaluation splits remain required.

Evaluate success rate, simulator calls to success, failure rate, and runtime across multiple seeds. Report training cost separately. Test new requirements on familiar circuits first, then hold out entire circuit structures or families as the collection grows. Group duplicate circuits together when splitting data.

Many tasks from one topology measure sizing ability within that structure. Simulation results establish performance under the recorded models and tests; manufactured-chip performance requires further evidence.

Use GPT-6 Astra (`gpt-6-astra`) through the signed-in Codex CLI initially for task generation, testing, and brainstorming; the user does not have separate API access. The `codex` provider uses ChatGPT login. Once the workflow is stable, compare Gemini and open-source models using the same source material, verifier, and proposal budget. Compare accepted-task rate, difficulty, diversity, and cost per useful task.

Initial learner: Gemma 3 1B-IT; algorithm: LoRA with multi-turn GRPO. Start with one v6e TPU if the programme allocation supports it, four CPU simulator workers, four attempts per episode, and four episodes per update. Increase context and attempt budgets after profiling. Still to select: comparison optimizer, task distribution, final training budget, and allocated cloud project/TPU zone. Generating tasks and learning to solve them are separate model roles. Pilot targets were chosen during setup and must be frozen before model comparisons. Whether the method or dataset offers a new research contribution requires comparison with prior work.
