# Gemma analog-design pilot: training and matched evaluation

Two optimizer updates completed. Adapter weights changed, and checkpoint 2 passed readback verification.

| Metric | Original model | Trained model |
|---|---:|---:|
| Solved episodes | 0/8 | 0/8 |
| Mean final reward | -0.275474 | -0.157575 |
| Failed actions | 7/32 | 2/32 |
| Circuit evaluations | 32 | 32 |
| Actions changing parameters | 0 | 0 |

Higher reward is better; success earns +1. Failed constraints receive negative scores.

The trained model produced fewer failed actions in this sample, but neither model changed the starting parameters or solved a circuit. Useful circuit optimization is not demonstrated.

Two optimizer updates; two validation tasks with four sampled episodes each. No held-out test-set or unseen-topology claim. Local artifacts on an expiring TPU VM.

The existing TPU is scheduled to terminate at 2026-09-17 06:53 UTC. Preserve this run externally before then. No external backup was created.

The matched runs use identical task order, four-attempt episode limits, temperature, base seed, and per-call seed policy. Baseline evaluation reloads the original model; trained evaluation reloads the verified adapter. The separate initial validation inside training uses a different rollout schedule.

The training pilot used 16 episodes / 64 evaluations; the matched comparison used another 16 episodes / 64 evaluations. Previous attempts remain separate and used 16 episodes / 60 evaluations.

Evidence: `performance.json`, `scalar_metrics.json`, `train/training_result.json`, `baseline/rollouts.jsonl`, `trained/rollouts.jsonl`, and per-episode `trajectories/`.
