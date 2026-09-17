# Research pilot trajectories

**Outcome: no optimizer update completed.** The final attempt ran out of TPU memory at the first gradient update (34.64 GiB needed versus 15.75 GiB available per device). No trained checkpoint or improvement is established.

Experimental training diagnostic; independent expert review remains pending. These are local VM files, not an external backup.

Total reserved: **16 episodes / 60 evaluations**. Original cumulative cap: 20 episodes / 80 evaluations.

## Attempts and logs

- **research_pilot_004**: 0 episodes, 0 evaluations. [Trainer log](../research_pilot_004/trainer.log); [Authorization](../research_pilot_004/authorization.json); [Outcome](../research_pilot_004/attempt_summary.json).
- **research_pilot_005**: 4 episodes, 12 evaluations. [Trainer log](../research_pilot_005/trainer.log); [Authorization](../research_pilot_005/authorization.json); [Outcome](../research_pilot_005/attempt_summary.json).
- **research_pilot_006**: 12 episodes, 48 evaluations. [Trainer log](../research_pilot_006/trainer.log); [Authorization](../research_pilot_006/authorization.json); [Outcome](../research_pilot_006/attempt_summary.json).

## Episode index

| Run | Task | Evaluations | Final reward | Solved | Actions changing start | Trajectory |
|---|---|---:|---:|---|---:|---|
| research_pilot_005 | song_dacfc_frontier_7b5de669b6d475c9 | 3 | -0.05821688411040808 | False | 0 | [Read](episodes/research_pilot_005_a20583e0eefffa9aa49ac3b073c30461.md) |
| research_pilot_005 | song_dacfc_frontier_7b5de669b6d475c9 | 3 | -0.05821688411040808 | False | 0 | [Read](episodes/research_pilot_005_b42962d7bcf2bd581aa64bc4fd581d17.md) |
| research_pilot_005 | song_dacfc_frontier_7b5de669b6d475c9 | 3 | -0.05821688411040808 | False | 0 | [Read](episodes/research_pilot_005_05fc18d00106ab5f9255d2c1680fcd93.md) |
| research_pilot_005 | song_dacfc_frontier_7b5de669b6d475c9 | 3 | -0.05821688411040808 | False | 0 | [Read](episodes/research_pilot_005_a183acec2ad74088257eb4b50acef63e.md) |
| research_pilot_006 | song_dacfc_frontier_7b5de669b6d475c9 | 4 | -0.05821688411040808 | False | 0 | [Read](episodes/research_pilot_006_3c7b95e0c4d320b0daaf27b5a2a809c6.md) |
| research_pilot_006 | ramos_pfc_frontier_08107b5a4a2972ca | 4 | -1.0 | False | 0 | [Read](episodes/research_pilot_006_5747576a4fc4dd75d15b30830adb6cac.md) |
| research_pilot_006 | song_dacfc_frontier_7b5de669b6d475c9 | 4 | -1.0 | False | 0 | [Read](episodes/research_pilot_006_77e7dbd8417904e4f405ca789b65ef96.md) |
| research_pilot_006 | ramos_pfc_frontier_00f3831c949d08ca | 4 | -0.011120155256065113 | False | 0 | [Read](episodes/research_pilot_006_a4e20dbbaa7acd572ad3ebfcd73248c1.md) |
| research_pilot_006 | ramos_pfc_frontier_08107b5a4a2972ca | 4 | -0.056810114397376696 | False | 0 | [Read](episodes/research_pilot_006_c08a9f6f48b75612b73d4b28a7921f50.md) |
| research_pilot_006 | ramos_pfc_frontier_08107b5a4a2972ca | 4 | -0.056810114397376696 | False | 0 | [Read](episodes/research_pilot_006_1f61d7a1c09c29f7f2a78f8ba411384d.md) |
| research_pilot_006 | ramos_pfc_frontier_00f3831c949d08ca | 4 | -0.011120155256065113 | False | 0 | [Read](episodes/research_pilot_006_d842061239d499790ce8b742a6ad5aa4.md) |
| research_pilot_006 | song_dacfc_frontier_7b5de669b6d475c9 | 4 | -0.05821688411040808 | False | 0 | [Read](episodes/research_pilot_006_3e2527242636467224b4ac7e8172724d.md) |
| research_pilot_006 | ramos_pfc_frontier_08107b5a4a2972ca | 4 | -1.0 | False | 0 | [Read](episodes/research_pilot_006_9b90cf0600cbe081ed62027bdaa6d961.md) |
| research_pilot_006 | ramos_pfc_frontier_00f3831c949d08ca | 4 | -0.011120155256065113 | False | 0 | [Read](episodes/research_pilot_006_74cc0ef6ebd26ba3f2e6f7b40a6c7e33.md) |
| research_pilot_006 | ramos_pfc_frontier_00f3831c949d08ca | 4 | -0.011120155256065113 | False | 0 | [Read](episodes/research_pilot_006_9de1d32c15196e6052b7d0b4a0e85bda.md) |
| research_pilot_006 | song_dacfc_frontier_7b5de669b6d475c9 | 4 | -0.05821688411040808 | False | 0 | [Read](episodes/research_pilot_006_5e4d7321a2e086ed83460bb613235643.md) |

## Additional evidence

- Each run retains `train/generations.jsonl`: exact prompts and raw model replies.
- `train/trajectories/` contains per-episode machine-readable events.
- `worker/<episode_id>/` contains the evaluator trajectory and raw simulations.
- `train/metrics/trajectory_log_*.csv` records Tunix trajectory status and token masks.
- Attempt 005 logged string prompt lengths as `prompt_count`; raw prompt strings are intact. This logging defect was fixed before attempt 006.
- Attempt 006 retains `source_snapshot/` and `config.json` for the executed code and configuration.

## What to improve next

1. Reduce gradient memory by splitting sequences into smaller microbatches and accumulating gradients while preserving the four-sample GRPO comparison.
2. Profile token padding. Observed maxima were 3,433 prompt tokens and 2,387 trajectory tokens, but other tasks may need more.
3. Address copying: every saved action retained the starting values or misspelled a parameter. A prompt example containing all current values may encourage copying; this is a hypothesis to test. Consider a small training-only supervised warm-up if prompt changes do not produce valid parameter exploration.
4. After a successful bounded update, verify finite losses, changed weights, checkpoint readback, and matched before/after performance before increasing the training budget.

A checkpoint file alone does not prove learning. Inspect the final outcome, weight-change checks, reward variation, and checkpoint readback before drawing conclusions.
