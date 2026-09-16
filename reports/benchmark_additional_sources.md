# Additional benchmark sources and exclusion decisions

Reviewed 2026-09-10. This review concerns complementary circuit classes; the separate amplifier review selects AnalogGym amplifier topologies. No additional source below is qualified as a final task by this report.

## Recommendation

Build the current 250-task release from circuit families that have verified references under the existing evaluator. Do not add a regulator, oscillator, reference, or PLL solely to increase the advertised number of circuit types. The original Li–Carusone differential-pair LDO is the strongest follow-on candidate, but its released nominal design does not yet meet the paper's full specification set. It needs a separate evaluator and a newly verified reference. The current release should describe its scope as amplifier sizing if no other class completes that work.

## Primary sources inspected

| Source | Inspected evidence | Suitability |
|---|---|---|
| [AutoCkt, DATE 2020](https://arxiv.org/abs/2001.01808), [author repository](https://github.com/ksettaluri6/AutoCkt) | Local pinned release `a6c8a61d3dffb8b433f19251e135994a5b0f6ee4`, original 45 nm netlist and model, YAML design space; existing project Figure 6 transcription notes | Directly released 45 nm design is useful for later process-transfer evaluation. It has the same two-stage topology as the current SKY130 adaptation, so it must not count as a new topology. |
| [AnalogGym, ICCAD 2024](https://arxiv.org/abs/2409.08534), [author repository](https://github.com/CODA-Team/AnalogGym) | Paper Table 1 and testbench section; local pinned release `0a9d1390ade361e2b4a2d33181e22367edbb8afc`; Basic_LDO netlist, parameters, AC/DC and transient source | Open PDK support is specifically provided for amplifiers and LDOs. Voltage-reference, sensing-front-end, and PLL families cannot be assumed to work with this project's open models. Local Basic_LDO fails its nominal heavy-load simulation. |
| [Li and Carusone, ICCAD 2023](https://www.zonghaoli.com/docs/ICCAD2023.pdf), [author repository](https://github.com/ChrisZonghaoLi/sky130_ldo_rl) | Full Figure 2/Table I page rendered and visually inspected; six SPICE source files fetched at `95adf17fc59804000ef6dc901241f0d4c7c419a1`; fresh simulations | Best complementary family for future integration. Published LDO1 uses a differential-pair error amplifier; LDO2 uses a folded cascode. Both released netlists use SKY130 high-voltage MOS devices, MIM capacitors and a process resistor. |
| [AnalogCoder, AAAI 2025](https://arxiv.org/abs/2405.14918), [author repository](https://github.com/laiyao1/AnalogCoder) | Paper benchmark page rendered and visually inspected; released `problem_set.tsv` read | Good source for later functional circuit-generation tasks (current mirrors, integrators, oscillators, Schmitt triggers). Its topology-complexity labels do not calibrate fixed-topology sizing difficulty. The paper's illustrative device model is simplified; importing tasks is not a SKY130 reproduction. |
| [AnalogCoder-Pro author repository](https://github.com/laiyao1/AnalogCoderPro), [paper](https://arxiv.org/abs/2508.02518) | Repository benchmark and workflow description read | Extends the generation/optimization direction, but no netlist or verifier from this source was qualified here. Do not count its categories toward this release. |
| [AnalogToBi author repository](https://github.com/Seungmin0825/AnalogToBi) | Repository dataset layout and evaluation description read | Useful schematic/netlist corpus for later topology research. Graph/ERC validity and novelty are not proof of meeting a circuit-sizing specification. No individual circuit was qualified here. |

The paper selection favors an executable source, inspectable models, and measurable behavior. These are project selection criteria, not a claim that the sources are objectively the best analog design papers.

## Local AnalogGym LDO audit

Source files: `external/AnalogGym/RGNN_RL/simulations/LDO_TB.txt`, `LDO_TB_vars.spice`, `LDO_TB_ACDC.cir`, and `LDO_TB_Tran.cir`. Fresh smoke runs changed include paths and removed interactive `plot` commands only. They used ngspice 47 and the installed pinned SKY130 PDK. Both processes returned zero, while their logs and waveforms showed invalid behavior. Raw evidence is in `runs/benchmark_additional_sources_20260910/analoggym_ldo/`.

- Nominal supply is 1.8 V, reference 0.4 V, intended output 1.6 V through a 300 kΩ/100 kΩ divider, with 5 mA and 55 mA loads.
- At 55 mA, measured output is -4.51561 V. There is no measurable unity crossing; native gain-bandwidth and phase measurements fail. The load transient reaches about -4.652 V.
- At 5 mA and 1.8 V supply, output is 1.58203 V. This isolated point does not establish a working regulator across the documented range.
- The light-load AC section changes `Iload1` but not `Iload2`, so the independent PSRR circuit remains at heavy load. Its light-load PSRR label is wrong.
- The AC path is injected through a feedback divider, but the reported crossing uses output voltage. The loop return ratio and phase convention require independent derivation; reported amplifier output gain cannot automatically be treated as regulator loop gain.
- The supplied normalized line/load-regulation formulas divide by average output, which becomes negative in failing states. Without a positive-output validity check, a negative metric could incorrectly appear better than an upper-bound target.
- The dropout sweep reaches 3 V even though this modified circuit uses 1.8 V MOS devices. This is not the original paper's high-voltage-device implementation.

These failures disqualify the released local default as a reference. Tightening or loosening targets cannot fix missing measurements or invalid biasing.

## Original Li–Carusone LDO audit

Figure 2 has direct output feedback and ideal reference sources; Table I uses a 2 V supply, 1.8 V reference, and 10 µA–10 mA load range. The authors explicitly describe their requested specifications as hypothetical rather than guaranteed feasible. These conditions and the actual circuit differ from AnalogGym's local `Basic_LDO`; the two sources must not be conflated. The fetched original netlists match Figure 2's high-level structures on visual review, but a full pin-by-pin audit is still pending.

Original files were run after replacing machine-specific include paths, removing interactive plots, and omitting the device-observation export include after the analyses. No device dimensions, models, loads, or bias values were changed. Both completed in approximately three seconds without simulator error messages. Diagnostic waveform extraction gives:

| Released design | Vout at 2 V supply | Transient range | Loop diagnostic |
|---|---:|---:|---|
| LDO1: differential pair | 1.79314 V | 1.39068–1.98831 V | Unity crossings exist at both loads; approximate phase margins are 49° and 60° using coarse linear interpolation. |
| LDO2: folded cascode | 1.99654 V | 1.99654–2.00000 V | Loop magnitude is below unity throughout the recorded sweep. It is near the supply rail, not regulating to 1.8 V. |

These are exploratory diagnostics, not independently cross-checked benchmark metrics. LDO1 is a viable optimization starting point, not an accepted paper-spec reference. LDO2's default is not usable as a reference. Compact results and source SHA-256 records are in `benchmark/research/additional_sources/`; original downloaded sources are in ignored `external/benchmark_additional_sources_20260910/`, and raw smoke outputs are in ignored `runs/benchmark_additional_sources_20260910/`.

## Required regulator evaluator before task generation

1. Fix a regulator-specific test contract: reference voltage, input-voltage range, load range, load edge rates, settling band, startup protocol, corners, temperature, and simulation horizon. Record any departure from the paper.
2. Reject non-finite outputs, rail-clamped/off regulation, negative or nonphysical supply power, missing measurements, incomplete sweeps, and missing loop crossings where stability is a required metric. A zero simulator exit code is insufficient.
3. Measure steady-state output error at minimum/maximum load; line regulation at each load; load regulation; supply current and quiescent current after subtracting load and accounting for separate bias sources. Check each quantity independently with native measurements and Python.
4. Derive and validate the return-ratio sign/convention using the released Tian injection bench, check both loads, require unwrapped phase, and inspect every relevant crossing. Reconcile native interpolated measurements with the Python path. Compare loop predictions with transient stability.
5. Measure positive supply-to-output transfer magnitude over explicitly bounded PSRR bands at both loads. Reset the load on the actual PSRR instance. Use one documented dB convention consistently.
6. Measure overshoot, undershoot, and settling after both load transitions. Do not substitute the overall peak of a startup waveform or accept a transient that ends before settling can be established.
7. Define dropout using an explicit regulation threshold and admissible device voltage range. Do not infer it from a sweep that never reaches valid regulation.
8. Find multiple passing references, demonstrate failing but valid starts, rerun references with finer AC sampling and smaller transient steps, and test deliberately invalid/regulation-failing cases before scaling.

The nominal physical simulation cost appears modest; implementing and validating the measurement contract is the substantial work. The source evidence does not justify squeezing this into the release by relabeling existing amplifier metrics.

## Difficulty and benchmark interpretation

Use easy/medium/hard as measured search difficulty under fixed action and simulation budgets, not as a synonym for the number of transistors, the paper's claimed novelty, or the strictness of one target. Source-paper complexity categories can inform task descriptions but cannot establish that exactly 25 tasks are empirically hard. Keep a separate structural/topology label and report any provisional difficulty label explicitly.

Different parameter values, start states, targets, or technology models do not automatically create new circuit structures. Hold related specifications and reference families together during splitting, and reserve topology-held-out evaluation for actual topology generalization claims.
