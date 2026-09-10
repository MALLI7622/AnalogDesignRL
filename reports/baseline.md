# Pilot results — 2026-09-08

Historical baseline from the initial evaluator. See [current verification results](verification.md) for the later independent measurement checks and regressions.

Two distinct amplifier topologies now have reproducible passing references and failing-start sizing tasks. These are project-defined pilot requirements. They do not reproduce published performance numbers or establish model-training results.

Sources: [AnalogGym at the pinned revision](https://github.com/CODA-Team/AnalogGym/tree/0a9d1390ade361e2b4a2d33181e22367edbb8afc) and [AutoCkt, Figure 6](https://arxiv.org/pdf/2001.01808v2). The AutoCkt circuit was manually reconstructed and resized for SKY130; its original process was a 45 nm predictive model. Per-circuit `source.json` files record the assumptions.

**Passing reference measurements**

Both use ngspice 47, SKY130 TT models, a 1.8 V supply, 27 °C, and a 10 pF load.

| Metric | Fan SMC | AutoCkt-derived two-stage |
|---|---:|---:|
| Low-frequency gain | 58.800 dB | 78.417 dB |
| Unity-gain frequency | 2.675 MHz | 2.804 MHz |
| Phase margin | 89.21° | 64.96° |
| DC supply power | 1.474 mW | 0.423 mW |
| DC follower error | 10.839 mV | 0.416 mV |
| Maximum steady tracking error | 14.826 mV | 0.493 mV |
| Rise / fall settling time | 0.298 / 0.122 µs | 0.142 / 0.132 µs |
| All eight constraints | Pass | Pass |

The common thresholds are gain ≥50 dB, unity-gain frequency ≥1 MHz, phase margin ≥60°, and both settling times ≤2 µs. Fan SMC permits 2 mW power and 20 mV DC/tracking error; the two-stage task permits 1 mW and 2 mV. The Fan tolerance is deliberately lenient for this pilot; its offset is measured and retained.

AC measurements use DC feedback to establish the operating point, with the feedback opened for small-signal analysis. Unity crossing and phase are interpolated on a logarithmic frequency grid. Separate follower simulations apply 0.3→0.5→0.3 V to Fan SMC and 0.85→0.95→0.85 V to the two-stage circuit. Settling means staying inside the stated absolute error band through the rest of the observation window, with at least 2 µs of settled samples. See the task JSON files for all test settings.

**Sizing tasks and random-search baseline**

Fan SMC starts with a 20 pF compensation capacitor and reaches only 0.669 MHz. Its passing reference uses 5 pF. The two-stage circuit starts at 5 pF compensation and 40 µm output PMOS width: phase margin is 35.87°, and the response misses the 2 mV settling band. A 20 pF capacitor and 80 µm width pass without changing the requirements.

Random search samples each allowed range uniformly in linear units, with uniform integer sampling for multiplicity. Each episode includes the initial evaluation and stops at the first passing design or 30 evaluations.

| Circuit | Evaluations to success, seeds 0 / 1 / 2 | Simulator invocations | Successes |
|---|---|---|---|
| Fan SMC | 7 / 5 / 3 | 14 / 10 / 6 | 3 of 3 |
| Two-stage | 14 / 7 / 19 | 28 / 14 / 38 | 3 of 3 |

These six episodes validate the loop and provide a small baseline. They are not a held-out benchmark. Complete reference results, baseline counts, runtimes, failure counts, and code hashes are saved in [baseline.json](baseline.json). Raw waveforms, simulator logs, and edit–feedback trajectories remain under the ignored `runs/validated/` and `runs/random_baseline/` directories. Reproduction commands are in [README.md](../README.md); repeat random search with seeds `0`, `1`, and `2` for both sizing tasks.

**Validation and limits**

All 16 unit tests passed, including analytic AC checks, settling after the last excursion, missing/nonfinite data, timeouts, invalid actions, and budget enforcement. Four integration evaluations confirmed that both references pass and both starting designs fail. All recorded simulations retain the supplied PDK's subcircuit-multiplier warning in their logs.

The shared AnalogGym testbench inspected during setup selects another amplifier and different conditions; its transient output/measurement assumptions also needed correction. This project uses its own fixed testbenches and metric extraction. A full independent Fan schematic connection audit remains pending.

Validation currently covers only the recorded nominal conditions. Process/voltage/temperature sweeps, mismatch, noise, CMRR, PSRR, layout parasitics, and manufactured-chip behavior remain outside this pilot. Next: create feasible task variants, freeze evaluation splits, and select the model and comparison optimizer before RL training.
