# First constrained TPU check

Four calls completed: no misspelled keys; all passed token alignment. Both first actions exceeded CAPACITOR_1 bounds. Both follow-ups were accepted but returned the starting values. There were zero accepted parameter changes, four simulator invocations, and identical final rewards (-0.011120155256065113). No optimizer updates.

This confirms spelling prevention in this small sample, not useful exploration. [Results](results.json) and [readable trajectories](trajectories.json).

This run used the original top-50 sampler and the decoder snapshot in `decoder_at_launch.py`. The subsequent probability integration removes top-k filtering to match the distribution used in GRPO; its separate run is `../constrained_integration_001`.
