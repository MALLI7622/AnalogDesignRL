# Starting-values example ablation

Removed only the repeated current-values answer example from every observation prompt. Starting values remained visible in the task and feedback. The system prompt and all other wording remained unchanged. This is the opt-in rollout variant `no_answer_example_v1`; the default prompt is unchanged.

The four-call run completed within its five-minute limit on the existing TPU. Compared with `formatter_smoke_001`, task, restored checkpoint, other configuration, and all four generation seeds matched. CPU checks verified the exact prompt deletion; three prompt comparison tests passed.

| Result | With example | Without example |
|---|---:|---:|
| Accepted actions | 4/4 | 0/4 |
| Accepted parameter changes | 0/4 | 0/4 |
| Token alignment passed | 4/4 | 4/4 |
| Solved episodes | 0/2 | 0/2 |
| Simulator invocations | 8 | 0 |

Without the example, both first attempts changed numeric values, but assigned CAPACITOR_1 = 2.52e-10 F above its maximum of 1e-10 F. Both second attempts used the nonexistent name CAPACATOR_0 instead of CAPACITOR_0. All replies were JSON objects; these were parameter-validation failures, not Markdown/JSON parsing failures. Final scores were -1 in both episodes.

This small paired sample supports the hypothesis that the answer example encourages copying. Removing it alone did not produce usable exploration. It does not establish general behavior across tasks or the base model. No automatic retries, expanded rollout, optimizer updates, or cloud resource changes followed; the worker was stopped on exit. Idle allocation billing is separate from process execution.

[Comparison JSON](results.json), [readable trajectory JSON](trajectories.json), [raw generations](rollout/generations.jsonl), [executed command](run.sh).

A possible next controlled test is one explicit instruction to change a single allowed parameter within its own bounds, keeping exact names and omitting unchanged parameters. That test has not been run. Do not train on this variant's all-invalid results.
