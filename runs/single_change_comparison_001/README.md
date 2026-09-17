# Explicit single-parameter instruction: bounded TPU comparison

The added instruction produced valid parameter changes on both first attempts, but no successful measurement or solved task. Each follow-up repeated the same candidate despite failed feedback. This is a narrow behavioral improvement, not evidence of useful optimization or learning.

Both explicit episodes returned `{"CAPACITOR_0": 2.52e-10}` twice. The first action changed the starting value `6.183e-11` to its permitted upper bound; the second changed nothing. All four actions passed numeric bounds and reached ngspice. The rendered `parameters.spice` files confirm the edit. All four evaluations failed AC extraction with `Expected positive low-frequency gain and non-inverting phase.`, yielding reward -1. The public feedback exposed only the existing generic simulation/action error and an empty failed-requirements list; the specific extraction diagnostic was not shown to the model.

| Outcome | Existing no-example prompt | Explicit instruction |
|---|---:|---:|
| Actual model responses | 3 | 4 |
| Actions submitted to evaluator | 2 | 4 |
| Valid actions admitted to simulation | 1 | 4 |
| Applied parameter-changing actions | 0 | 2 |
| Successful complete measurements | 1 | 0 |
| Solved episodes | 0 | 0 |
| ngspice invocations | 2 | 8 |

The baseline's first action exceeded CAPACITOR_1's bound, its second returned starting values, and its third contained duplicate keys. The existing validation guard aborted that arm before submitting the duplicate-key response. There was no retry. Thus the planned comparison of four responses per arm is incomplete: only three seeds are paired across arms. The baseline's episode 2 has no evaluator result, not a score of zero. Exact-key decoding prevents misspelled names but does not prevent generation of duplicate keys; final validation rejects them.

The same research_pilot_007 checkpoint, task, four-device TPU, float32 computation, exact-key decoder, temperature 0.9, token limits, and paired seed policy were used in both arms. Each episode allowed two attempts. Configurations differ only in `prompt_variant`; initial task and feedback rendering are identical. `single_change_v1` appends the starting-design failure statement, exact-one-change instruction, per-parameter bounds instruction, and feedback instruction to the existing system prompt. It remains an opt-in rollout variant; training behavior/defaults are unchanged.

Seven model responses, ten simulator invocations, seven passing token-alignment checks, and **zero optimizer updates**. No extra likelihood forwards were requested. Both runner processes and their workers exited. No resources were provisioned or extended.

[Results](results.json), [baseline trajectories](baseline/trajectories.json), and [explicit-prompt trajectories](explicit/trajectories.json) are formatted JSON. Each arm also contains individual task/episode JSON files, original JSONL logs, and its configuration. `analyze.py` regenerates the summaries, distinguishing valid action admission from successful measurement extraction. `run.sh` records the original plan; `run_explicit.sh` ran the remaining arm after the baseline abort. Source hashes and the prompt implementation snapshot preserve launch provenance.

The prompt cleared the first-action validity hurdle on this one task. It did not clear measurement validity or feedback-driven adaptation. Before a larger training run, test recovery from failure and distinguish valid-but-unsuccessfully-measured designs from invalid actions in public feedback, without exposing private evaluator artifacts. No further TPU experiment was launched automatically.
