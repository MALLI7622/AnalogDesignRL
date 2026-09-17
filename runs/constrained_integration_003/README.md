# Full-precision TPU probability diagnostic

Completed on four TPU devices, using the restored Gemma checkpoint and float32 model/cache computation with highest matmul precision. One generated response, three teacher-forced likelihood/loss forwards, zero optimizer updates.

The 116 sampled tokens passed the predeclared probability limits: maximum absolute log-probability error 0.000466272; mean 0.0000110617. The actual Tunix GRPO loss and entropy were finite, reference probabilities were finite, and the mean actor/sampled probability ratio was 0.999989748.

The generated parameter names were correct. The action was rejected because CAPACITOR_1 = 2.52e-10 F exceeds its 1e-10 F maximum; no simulation ran. The test does not establish useful exploration, training quality or full training-memory feasibility.

The preceding bfloat16 checks failed with maximum differences near 0.257, including after correcting temperature division. This result supports a reduced-precision explanation, but this diagnostic used a one-attempt episode instead of two and produced a different response. It is not a perfectly matched precision ablation and does not establish parity for every task or sequence length.

[Results](results.json), [trajectory JSON](trajectories.json), [per-token diagnostics](rollout/probability_checks.jsonl), [generation tokens and log-probabilities](rollout/generations.jsonl). The source files in this directory are snapshots of the code loaded by this process; later changes add training guards and metadata logging without changing the validated probability formula.
