# Larger exploration-prompt comparison

Both prompts used the same two-update Gemma 3 1B checkpoint on all 21 validation tasks, four independent episodes per task, and at most four attempts per episode. Seeds were paired by task, episode, and attempt. No optimizer updates were run.

| Metric | Current prompt | Exploration prompt |
|---|---:|---:|
| Solved episodes / 84 | 0 | 0 |
| Tasks solved at least once / 21 | 0 | 0 |
| Mean final reward (higher is better) | -0.106143 | -1.000000 |
| Circuit evaluations | 336 | 335 |
| Failed actions | 9 | 335 |
| Episodes stopped by context overflow | 0 | 1 |
| Replies rejected as invalid JSON actions | 0 | 335 |
| Simulator invocations | 654 | 0 |
| Valid actions changing starting parameters | 0 | 0 |
| Episodes with a valid parameter change / 84 | 0 | 0 |
| Tasks with a valid parameter change / 21 | 0 | 0 |
| Valid changed actions scoring better than the starting design | 0 | 0 |
| Episodes finding a better design than the start / 84 | 0 | 0 |
| Tasks with final-reward variation / 21 | 1 | 0 |

Paired episode rewards: {'worse': 83, 'tied': 1}.

**Outcome:** the combined exploration prompt failed. Every reply used Markdown fences and was rejected before simulation. The current prompt retained valid formatting but never changed the starting design. Neither variant demonstrated useful exploration. Keep the current default; next isolate a change-one-parameter instruction while preserving the working JSON scaffold. This comparison cannot distinguish the effects of the two prompt changes.

A valid changed action must pass the evaluator action/simulation checks and differ from the initial parameter vector. Repeated changed designs still count as changed actions; per-task unique design counts are in `results.json`. Reward variation can also come from failed actions.

Starting-design reference scores come from successful simulations of unchanged designs in the current-prompt arm. Better-than-start means a strictly higher fixed verifier score, not necessarily a solved circuit.

All 21 validation tasks are Ramos PFC; this is prompt tuning on validation, not a final held-out test. Same trained checkpoint in both arms. No optimizer updates.

Context-overflow episodes remain in the denominator; their last observed verifier score is retained. The exploration process initially aborted on overflow after 62 full episodes and three attempts in episode 63. A continuation skipped those 63 attempted pairs and ran the remaining 21 with the same settings and paired seeds. Subsequent overflows were logged per episode. No episodes were replayed and no context limits were enlarged.

The exploration variant changes the system instructions and removes the repeated current-value answer example from each observation. This tests the combined prompt change, not either component alone.

Evidence: `plan.json`, prompt configs, `source_snapshot/`, both `run.json` files, `rollouts.jsonl`, `generation_seeds.jsonl`, and full `trajectories/`.

Files are local to the TPU VM, scheduled to terminate at 2026-09-17 06:53 UTC. No external backup was created.

## Inspect the trajectories

[Searchable browser](review/viewer.html) · [Episode index and examples](review/README.md)

All 168 episodes and 671 exact replies are included, with prompts and evaluator feedback.

## Chat-template defect found during trajectory inspection

Inspection of `current_ramos_pfc_frontier_00f3831c949d08ca_ep4` confirmed all four raw model replies repeat the six starting values, and feedback parameters match those replies. The evaluator did not discard a changed proposal. However, comparison against the pinned local tokenizer template found missing BOS and missing completed-assistant end-of-turn markers in recorded prompts. See [chat_template_diagnostic.json](chat_template_diagnostic.json). This is a serialization mismatch; its causal contribution to copying is untested. Correct and validate the chat serialization before further prompt comparisons or training. Previous results describe behavior under the old serializer and should not establish that prompt wording or model ability alone caused the failures. No serializer fix or new experiment has been performed yet.

## Subsequent CPU audit

The chat serializer was repaired and validated without another model run. See [audit and limitations](../cpu_reliability_audit_001/README.md). The results here remain historical results under the old serializer.
