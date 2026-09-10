# Verification audit — 2026-09-08

Initial load/numerical audit. See [current verification results](verification.md) for the completed automated suite and remaining reviews.

The current verifier supports a nominal-condition sizing pilot. Independent circuit/measurement validation and production qualification remain incomplete. No model has been trained.

Three fresh evaluations used the passing SKY130 two-stage reference with identical device sizes, compensation, bias, supply (1.8 V), temperature (27 °C), and process corner (TT). Only the settings shown below changed.

| Case | Load | AC points/decade | Maximum transient step | Phase margin | All constraints |
|---|---:|---:|---:|---:|---|
| Original settings | 10 pF | 100 | 5 ns | 64.9647° | Pass |
| Finer numerical settings | 10 pF | 500 | 1 ns | 64.9658° | Pass |
| Exploratory load stress | 20 pF | 100 | 5 ns | 58.0842° | Fail |

The phase-margin requirement is 60°. The 20 pF case fails this requirement; every other measured constraint passes. This load is outside the original task's 10 pF condition. It demonstrates limited operating coverage, rather than a false acceptance under the original specification.

Finer settings changed unity-gain frequency by about 0.00047%, phase margin by 0.00106°, and each settling time by 2 ns. This provides numerical-consistency evidence for one design and condition. It does not independently validate the physical model, all measurement methods, or the full parameter space.

Exact tasks, measurements, checks, and provenance are in [verification_audit.json](verification_audit.json). Raw data are under `runs/verification_audit_20260908_232530/`. Each saved task can be rerun with `python3 scripts/evaluate.py --task <saved-task.json>`.

Current gaps: author-result reproduction; independent schematic and measurement review; process/supply/temperature and mismatch characterization; broader load and input tests; model-validity checks; enforced isolation of evaluator files from agent writes; and held-out task evaluation. The two small task instances have not established a challenging research benchmark.

Training admission requires evidence from independently checked passing and failing designs, numerical convergence, an explicit operating envelope, and audited reward handling. A larger reward must never substitute for passing all mandatory constraints. A second simulator can add evidence if compatible models are available, but agreement between two tools does not prove silicon behavior.

Production additionally requires a physical implementation, foundry-accepted design verification, simulation with extracted wiring effects, product-specific reliability/yield checks, and silicon characterization. These are outside the current evaluator. SkyWater's [public PDK status](https://skywater-pdk.readthedocs.io/en/main/status.html) describes the open release as experimental; the exact model and verification flow would need qualification with the intended fabrication provider.
