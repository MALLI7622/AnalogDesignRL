# Two-update research pilot

Status: stopped before any optimizer update. GRPO reward processing failed with a missing prompts field; see attempt_summary.json and the consolidated review in ../research_pilot_review/README.md.

This is an experimental runtime test. Independent expert review remains pending.
The cumulative cap is 20 episodes / 80 evaluations on the existing TPU.
The earlier attempt in `../research_pilot_004/` stopped before any episode or evaluation.

## Files to inspect

- `train/trajectories/*.jsonl`: one file per episode, with initial specification, raw model responses, parsed actions, simulator feedback, rewards, and closure. Each event is appended immediately.
- `train/generations.jsonl`: exact model prompts and replies, with sampler call index and seed. Requests are saved before generation, including failed or interrupted calls.
- `train/generation_seeds.jsonl`: compact sampling-seed ledger.
- `worker/<episode_id>/episode.json`: evaluator-side trajectory. Match its directory to the episode ID in the learner trace.
- `worker/<episode_id>/evaluation_*/`: simulator results, testbenches, and logs.
- `train/run.json`: pinned model, selected tasks, configuration, and authorization provenance.
- `train/metrics/`: training metrics.
- `trainer.log` and `worker.log`: console output and failures.
- `train/training_result.json`: written only if the trainer's update and checkpoint checks complete successfully. Its absence must not be reported as a successful training run.

These are local VM files. They have not been copied to external storage.
