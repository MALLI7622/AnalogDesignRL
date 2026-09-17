# Exact parameter-key constrained decoding

Historical implementation note: the evaluation-only restriction below has been superseded by the [training integration and TPU report](../constrained_training_cpu_001/README.md).

Implemented in `training/constrained_json.py` and enabled by default for `training.train --mode rollout`. Set `constrained_parameter_keys: false` explicitly to reproduce historical unconstrained decoding. Effective mode and decoder source hash are written to `run.json`.

A task-specific finite-state grammar permits flat JSON numeric objects with only names from the public task parameter schema. It compiles literal ASCII tokenizer pieces into transitions, masks illegal next-token logits before Tunix greedy/top-p selection, and allows the end-of-turn token only after a complete object. It uses a fresh sampler subclass with its own JIT closures; installed dependencies are not patched. No names or values are silently corrected. Sampling probabilities are computed after masking.

The returned text is checked before evaluator submission. Truncation, duplicate keys, or a decoding mismatch abort the rollout after preserving raw generation logs. No automatic retry is added. The existing simulator validation still enforces numerical bounds, integer parameters and finite values. Empty objects and unchanged values remain allowed; this feature does not guarantee exploration or success.

CPU verification uses the pinned local Gemma tokenizer and actual JIT-compiled Tunix `_sample`, with synthetic logits and no model weights. An adversarial test gives the misspelled continuation a higher score than the correct one: greedy and stochastic sampling both block it. Other checks cover valid numeric JSON, wrong names, truncated JSON, special/EOS placement, schema isolation and the training gate. See `tests_final.log` and `decoder_tests_final.log`. The first broader test attempt (`tests.log`) hit the sandbox's socket restriction; the final CPU-only run outside that restriction passed.

No TPU inference or training was launched for this implementation. Real-model constrained behavior and TPU compilation/performance remain untested. The grammar currently reconstructs its state from the generated prefix each sampling step; this favors a small implementation over optimal long-output decoding performance.

Constrained decoding is currently evaluation-only. Explicitly requesting it in a training mode fails before worker access or model loading, because GRPO's actor/reference likelihood calculations do not yet apply these grammar masks. Existing training defaults remain unconstrained. Enabling this decoder is not evidence that training is ready.
