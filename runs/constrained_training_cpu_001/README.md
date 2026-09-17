# Constrained training integration — 2026-09-17

Implemented shared grammar-masked probabilities for generation, actor loss, reference likelihoods and the old/anchor policy. This work included real TPU model runs as well as CPU regression tests.

## TPU results

| Run | Generated responses | Outcome |
|---|---:|---|
| [Constrained action check](../constrained_smoke_001/README.md) | 4 | No misspelled names; two valid actions, both unchanged; two out-of-bounds actions |
| [Bfloat16 integration](../constrained_integration_001/README.md) | 1 | Stopped before evaluation/update; max log-probability discrepancy 0.25719 |
| [Temperature fix, bfloat16](../constrained_integration_002/README.md) | 1 | Stopped before evaluation/update; max discrepancy 0.25723 |
| [Float32 diagnostic](../constrained_integration_003/README.md) | 1 | Probability/loss checks passed; max discrepancy 0.00046627; action exceeded a bound |

Total: seven generated responses and nine teacher-forced likelihood/loss forwards on the existing TPU allocation. No real-model optimizer updates. The four-call check used top-50 sampling; integration checks used full grammar-masked sampling. The precision diagnostic also changed the episode budget, so these runs are not a single-variable behavioral comparison.

## Implementation

- Grammar masks use each public task's exact allowed names. Mixed training schemas have their own token-ID columns and masks.
- Sampling uses top_k=0 and top_p=1.0. Actor/reference/anchor probabilities use the same grammar and float32 temperature division.
- Explicit metadata distinguishes generated tokens from environment feedback and padding, including multiple turns. Invalid/truncated responses and disagreement with the loss mask fail before updates.
- The learner checks actual sampled versus anchor probabilities before an optimizer update. The original 0.1 maximum and 0.01 mean log-error limits remain in force. Missing/nonfinite evidence also stops training. No importance correction or relaxed threshold hides a mismatch.
- Constrained decoding is now the default. Constrained training requires explicit `constrained_compute_dtype: "float32"`; bfloat16 failed the TPU check. `constrained_parameter_keys: false` explicitly selects historical unconstrained behavior.
- Both current and no_answer_example_v1 prompts are supported in the training environment, consistently for initial observations and feedback. Paired evaluation seeds remain rollout-only.
- Raw token IDs and generation log-probabilities are retained for subsequent audits. Dependency files were not patched; the Tunix probability dispatch is scoped and restored on exit.

## Verification and remaining limits

[Final regression log](complete_tests.log): 72 tests passed. Tests exercise the actual installed GRPO loss and gradients on a tiny CPU model, actor/reference agreement, forbidden-token gradients, multi-turn masks, invalid/truncated JSON, dtype normalization, prompt routing and precision/update guards. An adversarial bfloat16-logit CPU regression failed before the temperature fix and passed after it. The subsequent [five-test microbatch check](microbatch_tests.log) verified metadata binding to the exact inference slice used by RLCluster; it overlaps the 72-test suite, not five additional distinct tests.

The float32 TPU check evaluated the actual model's likelihoods and GRPO loss; it did not compute real-model gradients or update weights. Full float32 training-memory use is untested. Raw generation batches must share a schema; teacher-forced training metadata supports mixed schemas. Packed sequences and image inputs are unsupported.

Useful training remains blocked by behavior: no accepted parameter-changing action has been demonstrated in these checks. Numerical bounds and integer validity are still enforced by the evaluator, not the key grammar. A concrete next diagnostic is bounded one-parameter proposals or a numeric action grammar; neither is implemented or launched here. Existing qualification and research-manifest authorization requirements remain intact.
