# Verification results — 2026-09-08

**Automated audit: passed. Training approval: pending. Production qualification: absent.**

Run `python3 scripts/setup.py` followed by `python3 scripts/verify.py` from the repository root. Exact results and code hashes are in [verification.json](verification.json); raw decks, logs, and waveforms are in `runs/verification_final/` for this run.

| Check | Result |
|---|---|
| Unit tests, including malformed data, budgets, and training gate | 24 passed |
| Analytical AC and finite-ramp settling fixtures | 4 expected outcomes confirmed |
| Released AutoCkt setup and positive/negative circuit fixtures | 8 expected outcomes confirmed |
| Existing pilot references and failing starts | 4 expected outcomes confirmed |
| Previously passing random-search solutions | 6 still pass |
| Borderline measurement disagreement and refinement | 2 expected outcomes confirmed |

There were no unexpected acceptances or rejections in these 24 simulator cases. This finite challenge set does not establish an error rate over the entire design space.

**Original source and results**

The audit pins [AutoCkt commit a6c8a61](https://github.com/ksettaluri6/AutoCkt/tree/a6c8a61d3dffb8b433f19251e135994a5b0f6ee4) and checks the netlist, 45 nm model, and specification-file hashes. The exact-release run changes only the model include path. The instrumented variant adds measurements, makes capacitor parameter syntax explicit, and fixes temperature at the original simulator default of 27 °C. Its full complex AC response agrees with the original to a maximum relative difference of 3.18e-8; supply current differs by 2.70e-9 relative.

The released default sizing produces **0.631 V/V gain** and fails the paper's minimum target range of 200 V/V. It remains a failing fixture. We did not resize it and label the result a reproduced author solution.

A separate project-sized fixture, within the authors' discrete action ranges, passes a target selected from their published ranges:

| Metric | Measured | Requirement |
|---|---:|---:|
| Gain at 1 Hz | 284.876 V/V (49.093 dB) | ≥200 V/V |
| Unity-gain frequency | 2.628 MHz | ≥1 MHz |
| Phase margin | 60.802° | ≥60° |
| Total supply current | 78.507 µA | ≤1 mA |

Conditions are 1.2 V, 27 °C, and 10 pF, using the released predictive model. Dimensions and all edits are recorded in [autockt_cases.json](../verification/autockt_cases.json). The fixture uses the source's 0.5 µm unit widths and 90 nm lengths, with specified device multiplicities. This validates a feasible design for these four tests. The paper's aggregate RL success rates and individual manufactured designs have not been reproduced.

**What was strengthened**

Python extraction is cross-checked against ngspice's native measurements. The existing SKY130 evaluator checks all eight required metrics and rejects disagreement, including conflicting pass/fail decisions within the numerical comparison tolerance. The two paths share a simulator and device models; they are independent measurement implementations, not independent physical validation.

Analytical RC fixtures check gain, unity frequency, phase margin, and settling. Deliberate circuit failures cover insufficient compensation, reversed inputs, a shorted output, and a missing model file. Software checks reject incomplete outputs, NaNs, ambiguous unity crossings, and missing transient samples. A borderline phase-margin case is rejected on the coarse grid and resolves with finer sampling.

Increasing the source fixture's AC density fivefold and tightening solver tolerances changes gain by 1.15e-6 dB, unity frequency by 1.44 Hz, and phase margin by 0.000248°. Comparison tolerances are declared in [crosscheck.py](../analog_design/crosscheck.py).

**Coverage and review still required**

Five exploratory, single-axis changes were also simulated. The fixture passes at 1.1 V, 1.3 V, 0 °C, and 85 °C with other conditions nominal; doubling the load to 20 pF reduces phase margin to 51.94° and fails. These samples do not qualify the combined operating range. The supplied predictive model does not provide a characterized foundry corner/mismatch dataset for this audit.

An independent analog reviewer should confirm the circuit and testbench interpretation, the model's applicable limits and retained BSIM warnings, and which requirements are sufficient for the intended task. Process variation, mismatch yield, layout effects, and production reliability remain unverified. Agent/evaluator process isolation and a held-out task protocol also remain pending.

Training-mode episodes now require an explicit review record bound to the task and evaluator hashes. [qualification.json](../verification/qualification.json) contains no approvals. Evaluation mode remains available for investigation; this gate does not itself provide OS isolation.
