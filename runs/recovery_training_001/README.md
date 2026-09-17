# Recovery feedback and bounded training pilot

The authorized pilot stopped before its first optimizer update. All four episodes in the first training group received reward **-1**, so the new reward-variation guard stopped before teacher-forced likelihoods or gradients. There is no evidence of learning, and no trained checkpoint or after-training evaluation. The second scheduled training task was not attempted.

The four training episodes used `song_dacfc_frontier_7b5de669b6d475c9`. Every first action changed CAPACITOR_0 from `3.627e-12` to `6e-12`; every follow-up repeated that same candidate. All eight actions were valid, reached ngspice, and failed AC measurement extraction because of **multiple unity crossings**. All four final rewards were identical, which would give zero GRPO reward-based advantages. Two episodes output the full parameter dictionary, violating the instruction to return only the changed parameter, although only CAPACITOR_0 actually changed.

| Evidence | Before evaluation | Training group |
|---|---:|---:|
| Completed episodes | 2 | 4 |
| Model responses | 4 | 8 |
| Valid actions | 4 | 8 |
| Applied parameter changes | 1 | 4 |
| Successful complete measurements | 2 | 0 |
| Solved episodes | 0 | 0 |
| ngspice invocations | 8 | 16 |

The validation tasks were the same two sorted Ramos IDs recorded in `evaluation_plan.json`, held out from optimizer inputs but previously used for development. One validation episode repeatedly failed AC extraction; the other repeated its starting design and scored -0.056810114397376696. These are baseline observations, not a before/after learning comparison.

## Implemented changes

- Worker feedback now exposes `action_valid`, `parameters_changed`, `measurement_success`, and a fixed `failure_category`. Raw subprocess messages, private paths, and evaluator artifacts stay server-side. Bounds, circuit requirements, simulation, and rewards are unchanged.
- `single_change_recovery_v1` adds instructions to make modest changes and choose a different candidate after failure. It is available for both training and rollout; existing defaults are unchanged.
- Explicit `--warm-start-adapter` initializes training from verified adapter weights with a fresh optimizer and step counter. It does not restore optimizer history or implement full training resume. The initial adapter hash is recorded after restoration. Actual TPU initialization matched checkpoint 007's verified adapter hash.
- The pilot checks reward variation before expensive likelihood/optimizer computation. The existing token-alignment and sampler/teacher probability guards remain in place. This run reached the reward guard first, so it does not validate real-model gradients or float32 optimizer memory feasibility.
- Teacher-forced likelihoods use the configured sequence microbatch size. External evaluation avoids duplicate internal validation rollouts for this pilot.

## Integration repair and resource accounting

The first training launch failed before producing any model response: Tunix's agentic path supplied a single prompt string, but `ConstrainedGeneration` iterated it as a list of characters. The fix normalizes the input for schema extraction while preserving the input passed to the sampler. A regression check now exercises both single-string agentic input and list-based rollout input.

The failed launch remains in `train/` and `train.log`; its worker reserved one empty training episode, generated zero responses, and performed zero evaluations or updates. The repaired continuation is in `train_continuation/`. Its separate manifest explicitly carries forward the four baseline responses already spent and allows only the remaining 20 responses; baseline was not repeated. Both worker manifests and usage records are preserved, without resetting the original usage ledger.

Actual total: **12 model responses**, one additional request rejected before sampling, **24 ngspice invocations**, and **0 optimizer updates**. All 12 responses passed token alignment. The original response cap was 24. No resources were created or extended. Both worker/runner invocations exited.

Tunix wrote an untrained step-0 snapshot while closing the failed training runs. It is not a newly trained checkpoint; positive-step checkpoint validation rejects it. The existing checkpoint 007 remains the last verified trained adapter. The after-evaluation command was not run because the stop guard prevented any update.

## Verification and artifacts

The 54-test CPU regression suite passed, including worker HTTP behavior, feedback sanitization, checkpoint controls, reward guard, constrained probabilities, and adapter routing. After the integration fix, all seven targeted constrained-training tests passed (55 distinct tests across both runs). The first sandboxed suite was blocked only by its local HTTP socket; the permitted rerun passed. `git diff --check` passed.

- [Machine-readable results](results.json)
- [Training trajectories, formatted JSON](train_continuation/trajectories.json)
- [Baseline trajectories, formatted JSON](before/trajectories.json)
- [Reward group and stop evidence](train_continuation/reward_groups.jsonl)
- Individual task/episode JSON files accompany each trajectory bundle.
- Source snapshots, manifests, configurations, run scripts, and raw generation/worker logs preserve provenance.

This experiment shows that clearer feedback and a recovery instruction did not produce reward diversity in this training group. Increasing optimizer steps alone cannot usefully reinforce a better candidate when no sampled candidate scores better. Further work needs a source of diverse, measurable candidates before another GRPO update is justified; this run does not establish that all other training tasks would have zero variation.
