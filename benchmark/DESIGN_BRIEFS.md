# Circuit design briefs

These are project-authored sizing exercises using fixed SKY130 amplifier circuits. The original papers motivate the architectures; project test conditions and numerical requirements are not reproductions of original-paper performance.

Choose allowed parameter values that satisfy all stated requirements together under the task's fixed supply, load, common mode, temperature, and test waveforms. The topology and all unexposed parameters remain fixed.

This is standalone educational material. It contains no task answers, passing parameter vectors, or optimization traces. Active task JSON remains the authority for allowed controls, requirements, and test conditions.

## Reading the controls

- Capacitance is in farads, current in amperes, resistance in ohms, and editable AutoCkt widths in micrometres. Use each task's explicit units and bounds.
- AnalogGym names containing gm1, gm2, or gm3 identify device groups. The editable M quantities are dimensionless integer multiplicities at fixed individual width and length; they are not transconductance values in siemens.
- Changing multiplicity or width changes capacitance, operating point, transconductance, and output resistance together. The resulting metric changes need not be monotonic over the full domain.
- A shared parameter changes every netlist instance that uses it. Numeric factors already present in an instance expression remain part of the fixed circuit relation.
- Fixed geometry does not imply fixed current or transconductance: those still respond to the bias and signal operating point.
- A bias reference current sets a mirror network. It is neither the total supply current nor a control over one isolated gain stage.

## Reading the measurements

| Metric | Meaning |
|---|---|
| `gain_db` | Open-loop gain at the AC sweep's starting frequency, in dB; the sweep and operating point are specified by the task. |
| `unity_gain_hz` | The single downward zero-dB crossing within the measured AC band, interpolated in log frequency. Missing or multiple crossings fail measurement validation. |
| `phase_margin_deg` | Phase margin at that crossing under the specified nominal small-signal test. It does not establish stability for every load, input level, or process corner. |
| `power_w` | Quiescent DC supply power at the tested common-mode operating point, rather than step energy or average dynamic power. |
| `dc_error_v` | Absolute follower output error relative to the input common-mode voltage at one DC operating point. It is not a mismatch-derived input-offset distribution. |
| `max_tracking_error_v` | Maximum absolute input/output error before the rising edge and over the final quarter of each observed plateau. It is not maximum error across the complete waveform. |
| `settling_rise_s_and_settling_fall_s` | Time measured from each edge start until the sampled output enters the specified absolute band and stays inside for the remainder of that edge's observation window, with at least minimum_hold_s observed inside. The band is settling_tolerance_v, separate from the tracking-error target. |

Nominal simulation checks do not provide layout, mismatch, process-corner, noise, CMRR, PSRR, or fabrication validation. Source review depth differs by paper and is recorded per topology.

## AutoCkt two-stage Miller amplifier

`autockt_two_stage`

Size a two-stage voltage amplifier with an NMOS differential input pair, a PMOS current-mirror load, a PMOS second stage, and an NMOS output current sink. A capacitor connects the first-stage output to the final output. The exercise couples gain, bandwidth, stability, quiescent power, and follower settling through physical device widths and bias current.

Source: [AutoCkt: Deep Reinforcement Learning of Analog Circuit Designs](https://arxiv.org/abs/2001.01808v2). Original full-paper text and the relevant original schematic were reviewed. The simulated circuit uses project or AnalogGym SKY130 adaptation and project-defined conditions.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CCOMP` | F | Miller capacitor between stage1 and vout; couples the two gain stages and changes both the small-signal frequency response and charging during a step. Instances: Cc. |
| `IBIAS` | A | Reference current injected into diode-connected NMOS Xmir; the shared bias node controls the fixed-geometry tail device and the editable output current sink. Instances: Iref. |
| `W_IN` | um | Shared width of the two NMOS differential-input devices; changes their transconductance, operating point, and capacitance together. Instances: Xin_left, Xin_right. |
| `W_LOAD` | um | Shared width of the first-stage PMOS current-mirror devices; changes their operating point and loading while retaining the matched width relation. Instances: Xload_left, Xload_right. |
| `W_GM` | um | Width of the PMOS second-stage gain device; changes output drive and the loading seen at stage1. Instances: Xgm. |
| `W_LOAD2` | um | Width of the NMOS output current sink; changes its relation to the fixed reference device, output operating point, and output capacitance. Instances: Xload2. |

- The reference and tail NMOS geometry, all device lengths, connections, and external load are fixed by the task.
- The two input widths move together, as do the two first-stage mirror widths; these are shared parameters, not independent device edits.

## Fan single-Miller three-stage amplifier

`Fan_SMC_Pin_3`

Size a three-stage amplifier with a PMOS input pair, a PMOS-driven intermediate current-mirror stage, and an NMOS output gain device. One outer Miller capacitor spans the first-stage output and final output. A fixed PMOS feedforward path also drives the output, so rising and falling responses depend on different device paths.

Source: [Single Miller Capacitor Frequency Compensation Technique for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2005.843602). Original-paper text and the released AnalogGym schematic were reviewed. The original figure image was not inspected.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Outer Miller capacitor from first-stage output net050 to VOUT; couples the first and output stages. Instances: C0. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_10_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm10. |
| `MOSFET_23_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm23. |
| `MOSFET_8_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm9, xm8. |

- The PMOS feedforward output device xm11 is fixed in geometry; its gate is net050, so its current still changes with circuit operation.
- The intermediate NMOS mirror and bias/cascode device groups retain their released geometry and multiplicity relations.

## Leung nested Miller amplifier with a nulling resistor

`Leung_NMCNR_Pin_3`

Size a three-stage amplifier whose two nested compensation capacitors return to the output through a shared nulling resistor. The shared node couples the two capacitor choices. The PMOS output load is bias-driven, while the NMOS output gain device is driven by the intermediate stage.

Source: [Analysis of Multistage Amplifier-Frequency Compensation](https://doi.org/10.1109/81.948432). Original full-paper text and the relevant original schematic were reviewed. The simulated circuit uses project or AnalogGym SKY130 adaptation and project-defined conditions.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Outer compensation branch between first-stage output net050 and shared return node net044. Instances: C0. |
| `CAPACITOR_1` | F | Inner compensation branch between shared return node net044 and intermediate-stage output net049. Instances: C1. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_10_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm10. |
| `MOSFET_23_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm23. |
| `MOSFET_8_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm9, xm8. |

- R0, controlled by fixed RESISTOR_0, connects net044 to VOUT; it remains part of both capacitor return paths and is not an editable control in this domain.
- PMOS output device xm11 has a bias-driven gate net013; it is a fixed-geometry current-source load, rather than the signal-driven feedforward PMOS used in several other domains.

## Leung damping-factor-control amplifier, variant 1

`Leung_DFCFC1_Pin_3`

Size a three-stage amplifier with an outer Miller capacitor and an auxiliary NMOS damping-control branch around the intermediate-stage output. The local capacitor is connected across the auxiliary transistor's gate and drain. Main-stage sizing changes the operating conditions of this fixed auxiliary branch as well as the signal path.

Source: [Analysis of Multistage Amplifier-Frequency Compensation](https://doi.org/10.1109/81.948432). Original full-paper text and the relevant original schematic were reviewed. The simulated circuit uses project or AnalogGym SKY130 adaptation and project-defined conditions.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Outer Miller capacitor between first-stage output net050 and VOUT. Instances: C0. |
| `CAPACITOR_1` | F | Local damping-control capacitor between net049 and net1, the gate and drain of auxiliary NMOS xm24. Instances: C1. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_11_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm11. |
| `MOSFET_25_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm25. |
| `MOSFET_9_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm10, xm9. |

- Auxiliary NMOS xm24 senses net049 and drives net1; its geometry is fixed.
- PMOS xm12 provides a fixed-geometry feedforward output path driven by net050.
- The original paper discusses DC robustness of the high-impedance damping-control node; nominal sizing tests do not establish robustness to process variation or mismatch.

## Leung damping-factor-control amplifier, variant 2

`Leung_DFCFC2_Pin_3`

Size a three-stage amplifier with the auxiliary damping-control branch placed at the first-stage output. This variant uses an auxiliary PMOS device and a local capacitor in addition to the outer Miller capacitor. Its connections differ from variant 1 even though the task exposes similar parameter categories.

Source: [Analysis of Multistage Amplifier-Frequency Compensation](https://doi.org/10.1109/81.948432). Original full-paper text and the relevant original schematic were reviewed. The simulated circuit uses project or AnalogGym SKY130 adaptation and project-defined conditions.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Outer Miller capacitor, instance C1, between net050 and VOUT. Instances: C1. |
| `CAPACITOR_1` | F | Local damping-control capacitor, instance C2, between net050 and net2, the gate and drain of auxiliary PMOS xm10. Instances: C2. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_11_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm11. |
| `MOSFET_25_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm25. |
| `MOSFET_8_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm9, xm8. |

- Auxiliary PMOS xm10 senses net050 and drives net2; its geometry is fixed.
- PMOS xm12 provides a fixed-geometry feedforward output path driven by net050.
- Capacitor instance suffixes and parameter suffixes differ: C1 uses CAPACITOR_0 and C2 uses CAPACITOR_1.
- The original paper discusses DC robustness of the high-impedance damping-control node; nominal sizing tests do not establish robustness to process variation or mismatch.

## Hoi Lee active-feedback three-stage amplifier

`HoiLee_AFFC_Pin_3`

Size a three-stage amplifier with a common-gate active-feedback branch and a replica-bias branch. One capacitor is connected across the output stage; the other feeds the output signal into the active-feedback transistor's source. The auxiliary devices and mirror are fixed, so main-stage sizing and bias must work with their existing loading and operating conditions.

Source: [Active-Feedback Frequency-Compensation Technique for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2002.808326). Original full-paper text and the relevant original schematic were reviewed. The simulated circuit uses project or AnalogGym SKY130 adaptation and project-defined conditions.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Inner compensation capacitor from intermediate-stage output net049 to VOUT, across the output gain stage. Instances: C0. |
| `CAPACITOR_1` | F | Active-feedback capacitor from VOUT to net1, the source of common-gate NMOS xm63; xm63's drain connects to first-stage output net050. Instances: C1. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_11_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm11. |
| `MOSFET_25_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm25. |
| `MOSFET_9_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm10, xm9. |

- Common-gate NMOS xm63 and replica NMOS xm60 share the gma geometry parameter; their gates use bias node VB3.
- PMOS mirror xm59/xm62 participates in the replica-bias arrangement. These devices and the feedforward output PMOS xm12 retain fixed geometry.
- CAPACITOR_0 is the inner branch in this netlist; it should not inherit the outer-capacitor description used for Fan or the Leung DFC variants.

## Peng AC-boosting three-stage amplifier

`Peng_ACBC_Pin_3`

Size a three-stage amplifier with an outer Miller capacitor and a separate capacitive AC path around the intermediate-stage network. The auxiliary branch shares a drive node with the intermediate current mirror and connects to its output through the second capacitor. This path affects frequency response while its capacitor blocks DC transfer between its two terminals.

Source: [AC Boosting Compensation Scheme for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2004.835811). Original-paper text and the released AnalogGym schematic were reviewed. The original figure image was not inspected.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Outer Miller capacitor between first-stage output net050 and VOUT. Instances: C0. |
| `CAPACITOR_1` | F | AC-boosting capacitor between intermediate output net049 and auxiliary node net1; net1 is driven by fixed NMOS xm24 and loaded by the local PMOS network. Instances: C1. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_11_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm11. |
| `MOSFET_25_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm25. |
| `MOSFET_9_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm10, xm9. |

- Auxiliary NMOS xm24 has gate net043 and drain net1. The similar-looking Leung DFCFC1 auxiliary device instead has gate net049.
- The diode-connected PMOS xm59 and bias PMOS xm8 load net1; their geometry is fixed.
- PMOS xm12 supplies a fixed-geometry feedforward output path driven by net050.

## Peng impedance-adapting three-stage amplifier

`Peng_IAC_Pin_3`

Size a three-stage amplifier with one outer Miller capacitor and a series RC branch from the intermediate-stage output to ground. The RC branch changes the impedance seen by the intermediate node, while the outer capacitor couples the first-stage output to the final output. The resistor and the two capacitors therefore describe different physical effects and must be considered together with stage sizing and bias.

Source: [Impedance Adapting Compensation for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2010.2090088). Original full-paper text and the relevant original schematic were reviewed. The simulated circuit uses project or AnalogGym SKY130 adaptation and project-defined conditions.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Outer Miller capacitance Cm between first-stage output VOUTP and VOUT. Instances: C0. |
| `CAPACITOR_1` | F | Impedance-adapting capacitance Ca between intermediate-stage output net10 and RC junction net4; it is in series with RESISTOR_0 to ground. Instances: C1. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_10_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm10. |
| `MOSFET_23_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm23. |
| `MOSFET_8_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm9, xm8. |
| `RESISTOR_0` | ohm | Impedance-adapting resistance Ra from net4 to GNDA. Together with CAPACITOR_1 and node capacitances it changes the intermediate-node frequency dependence; changing Ra does not move every pole in the same direction. Instances: R0. |

- PMOS xm66, labelled gmt in the released parameters, is a cascode/current-source load in the intermediate branch. It is not an additional independently controlled primary gain stage.
- The intermediate mirror, cascode devices, and feedforward output PMOS xm11 retain fixed geometry.
- The output gain transistor is driven by net10; net049 is an internal load node in this topology.

## Ramos positive-feedback-compensated three-stage amplifier

`Ramos_PFC_Pin_3`

Size a three-stage amplifier that combines an outer Miller capacitor with local positive capacitive feedback across the non-inverting intermediate stage. The local branch changes the intermediate dynamics and damping. Treating both capacitors as interchangeable stabilizing loads would miss the defining mechanism of this topology.

Source: [Three Stage Amplifier With Positive Feedback Compensation Scheme](https://doi.org/10.1109/CICC.2002.1012833). Original-paper OCR transcript and released schematic were reviewed. The original figure image was not inspected, and no quantitative claim relies on OCR-sensitive symbols or units.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Outer Miller capacitor between first-stage output net050 and VOUT. Instances: C0. |
| `CAPACITOR_1` | F | Local positive-feedback capacitor between net050 and intermediate-stage output net049; it spans the non-inverting intermediate stage. Instances: C1. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_10_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm10. |
| `MOSFET_23_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm23. |
| `MOSFET_8_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm9, xm8. |

- The intermediate PMOS signal device and NMOS current mirror form the non-inverting path bridged by CAPACITOR_1.
- The fixed-geometry PMOS output feedforward device xm11 is driven by net050.
- Larger capacitance does not imply greater stability for the local positive-feedback branch.

## Song Guo dual-active-capacitive-feedback amplifier

`Song_DACFC_Pin_3`

Size the released AnalogGym three-stage amplifier with an output-to-feedback-node capacitor, auxiliary active-feedback devices, and a shunt capacitor at the first-stage output. A separate PMOS input pair supplies an additional signal path into the intermediate stage. These descriptions follow the released netlist and schematic; the complete original journal paper was not available for review.

Source: [Dual Active-Capacitive-Feedback Compensation for Low-Power Large-Capacitive-Load Three-Stage Amplifiers](https://doi.org/10.1109/JSSC.2010.2092994). Original abstract and author bibliography were checked; original full text and original schematic were unavailable. Circuit roles here are grounded in the released AnalogGym netlist and schematic.

| Editable parameter | Unit | Physical role |
|---|---|---|
| `CAPACITOR_0` | F | Feedback capacitor from VOUT to internal feedback node net70; this node belongs to the fixed active-feedback network. Instances: C0. |
| `CAPACITOR_1` | F | Shunt capacitor, instance C2, from first-stage output net4 to GNDA. It loads net4 directly rather than bridging net4 and VOUT. Instances: C2. |
| `CURRENT_0_BIAS` | A | Reference current drawn through the PMOS bias network; changes bias conditions across multiple branches, including signal and auxiliary branches. Instances: I0. |
| `MOSFET_10_1_M_gm2_PMOS` | multiplicity | Multiplicity of the PMOS signal device driving the intermediate current-mirror network; changes its operating point and loading as well as small-signal gain. Instances: xm10. |
| `MOSFET_23_1_M_gm3_NMOS` | multiplicity | Multiplicity of the NMOS output gain device; changes output drive and capacitance while loading the intermediate-stage output through its gate. Instances: xm23. |
| `MOSFET_8_2_M_gm1_PMOS` | multiplicity | Shared multiplicity of the main matched PMOS differential-input pair; scales both devices at fixed individual width and length. Instances: xm9, xm8. |

- The gma1 PMOS pair xm5/xm6 and gma2 PMOS xm7 have gates at VOUTN and fixed geometry.
- The additional PMOS differential pair xm57/xm58, labelled gmf1, connects the inputs to net043/net049 and is separate from the editable main input pair.
- The output feedforward PMOS xm11 is driven by net4 and retains fixed geometry.
- Original-paper attribution was checked against the abstract and author bibliography. Full original schematic correspondence and the paper's analytical conditions remain unverified.

The machine-readable [design briefs](design_briefs.json) include exact instance terminals and netlist hashes. Netlist provenance and license terms are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Original-paper links identify sources; no paper text or figures are bundled here.
