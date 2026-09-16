# Paper and circuit selection evidence

Research date: 2026-09-10. This is a source-selection report, not a benchmark validation certificate. Final inclusion, task counts, performance targets and difficulty labels must be taken from the generated benchmark's validation results. Machine-readable paths, file hashes, parameter names and review limitations are in [`benchmark/research/paper_candidates.json`](../benchmark/research/paper_candidates.json).

## Defensible scope

The strongest available starting point is **nominal transistor-level amplifier sizing** using published circuit structures adapted to the open SKY130 1.8 V devices. The proposed ten circuits provide two-stage Miller compensation plus several three-stage compensation mechanisms. They do not constitute 250 different circuit topologies or comprehensive coverage of analog design. Comparator, oscillator, reference, filter, ADC, noise, mismatch and layout tasks need their own implementations and verifiers before being included.

The recommended wording for generated tasks is: “Size this released SKY130 adaptation of a published amplifier topology to meet these project-defined requirements under the stated simulation conditions.” Original-paper numerical results must not be presented as obtainable targets merely because a circuit name matches. None of the reviewed AnalogGym releases uses the original paper's transistor models. Some also change supply, load, bias generation and device sizing.

The pinned source is [CODA-Team/AnalogGym](https://github.com/CODA-Team/AnalogGym/tree/0a9d1390ade361e2b4a2d33181e22367edbb8afc), commit `0a9d1390ade361e2b4a2d33181e22367edbb8afc`. Its [`LICENSE`](../external/AnalogGym/LICENSE) is BSD-3-Clause; preserve its copyright, conditions and disclaimer with derivative netlists. Paper copyrights are separate. Research PDF copies and rendered pages under `runs/research_sources_20260910/` are working evidence, not benchmark assets to redistribute. The existing AutoCkt circuit is a project-authored reconstruction with separate provenance in [`circuits/autockt_two_stage/source.json`](../circuits/autockt_two_stage/source.json).

## Provisional core selection

| Circuit | Distinguishing mechanism | Released/project load; common mode | Evidence reviewed |
|---|---|---|---|
| `autockt_two_stage` | Two-stage Miller compensation; NMOS input | 10 pF; 0.9 V | Original full text and original Fig. 6; existing project netlist |
| `Fan_SMC_Pin_3` | Three-stage single outer Miller capacitor | 10 pF; 0.3 V | Original full text; released schematic and netlist |
| `Leung_NMCNR_Pin_3` | Nested Miller capacitors with common nulling resistor | 10 pF; 0.3 V | Original full text and Fig. 7(a); released schematic/netlist |
| `Leung_DFCFC1_Pin_3` | Active damping-factor control around intermediate stage | 100 pF; 0.3 V | Original full text and Fig. 7(c); released schematic/netlist |
| `Leung_DFCFC2_Pin_3` | Rearranged active damping-factor-control path | 100 pF; 0.3 V | Original full text and Fig. 7(d); released schematic/netlist |
| `HoiLee_AFFC_Pin_3` | Active capacitive feedback with feedforward paths | 100 pF; 0.3 V | Original full text and Figs. 6–7; released schematic/netlist |
| `Peng_ACBC_Pin_3` | Dedicated AC path parallel to second-stage DC path | 100 pF; 0.3 V | Original full text; released schematic and netlist |
| `Peng_IAC_Pin_3` | Outer Miller capacitor plus intermediate-node series RC | 150 pF; 0.24 V | Original full text, Figs. 1/3, equations and Table II; released schematic/netlist |
| `Ramos_PFC_Pin_3` | Local positive-feedback capacitor plus outer Miller path | 100 pF; 0.4 V | Original paper's full OCR transcript; released schematic/netlist |
| `Song_DACFC_Pin_3` | Two active capacitive-feedback paths and feedforward stages | 500 pF; 0.3 V | Original abstract and author bibliography; released schematic/netlist; supplementary analytical paper |

**Source limitation:** the original 2011 Song DACFC full paper was not recovered. It remains a provisional candidate with weaker source review. The original Fan and ACBC PDFs were readable as extracted text, but attempts to retrieve/render their original schematic pages failed; do not report those original figures as visually verified. Ramos's original four-page text was accessible as an OCR transcript, with corrupted formula symbols and some units; its original figure image was not verified. These distinctions are encoded per candidate.

The three Leung variants share a paper and substantial circuitry. Keep them in the same family for any paper- or topology-held-out split. Multiple target vectors on one circuit measure within-topology sizing generalization; they do not establish generalization to new analog circuit structures.

## Original sources and circuit interpretation

### Existing AutoCkt two-stage circuit

Settaluri et al., [AutoCkt: Deep Reinforcement Learning of Analog Circuit Designs](https://arxiv.org/abs/2001.01808v2), 2020, Fig. 6, was reviewed from the original local PDF. The diagram has eight MOS devices: an NMOS input pair with PMOS mirror load, an NMOS bias mirror/tail, a PMOS second stage, an NMOS output sink, and one Miller capacitor. The existing reconstruction follows that connectivity. Its original experiment used 45 nm predictive devices; the project's SKY130 models, geometry, 1.8 V supply and 10 pF load are explicit adaptations. `CCOMP`, `IBIAS`, and `W_GM` respectively control compensation, reference current and second-stage width. Previously passing pilot targets are useful regression evidence, not proof that a new target vector is meaningful or difficult.

### Fan SMC

Fan, Mishra and Sánchez-Sinencio, [Single Miller Capacitor Frequency Compensation Technique for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2005.843602), JSSC 40(3), 2005. [Original paper text](https://picture.iczhiku.com/resource/eetop/WYKhwFHGQpjORCXn.pdf), particularly Sections III–IV, distinguishes SMC from the additional feedforward path in SMFFC. This release is the three-stage SMC structure. `CAPACITOR_0` joins `net050` to `VOUT`; the separately biased PMOS feedforward output device supplies push-pull action. The stage multiplicities affect both transconductance and parasitics, so they should not be treated as independent ideal gm controls. Original conditions were 0.5 µm CMOS, ±1 V rails and a 120 pF load in parallel with 25 kΩ; the release's 10 pF load is materially different. Original text extraction drops some signs and formula symbols; the ±1 V supply is also stated explicitly in the authors' [conference precursor](https://citeseerx.ist.psu.edu/document?doi=bcea6c47da177fec14bce95e4c4c8fc0a4972b28&repid=rep1&type=pdf).

### Leung NMCNR and DFCFC variants

Leung and Mok, [Analysis of Multistage Amplifier-Frequency Compensation](https://doi.org/10.1109/81.948432), TCAS-I 48(9), 2001. [Original full paper](https://nthuee.org/archive/AIC/106%E8%AC%9D%E5%BF%97%E6%88%90/HW/Final%20project/reference/reference/01CAS-leung.pdf) Fig. 7 was rendered and checked. NMCNR uses two capacitors sharing `net044`, with `RESISTOR_0` between that node and the output. DFCFC1 adds the NMOS `gm4` branch around the intermediate stage; DFCFC2 relocates the damping network and uses a PMOS `gm4` branch. These are related but distinct compensation graphs. In DFCFC2, instance `C1` uses parameter `CAPACITOR_0`, while instance `C2` uses `CAPACITOR_1`; instance labels do not define parameter indices.

The original comparison used AMS 0.8 µm CMOS, ±1 V rails and 100 pF in parallel with 25 kΩ. Section VIII discusses a high-impedance DC node in the DFC circuitry and an optional local-feedback remedy. A nominal passing SKY130 design does not establish robustness of that node under mismatch or process variation. The released topology should be held fixed rather than silently adding the optional remedy.

### Hoi Lee AFFC

Lee and Mok, [Active-Feedback Frequency-Compensation Technique for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2002.808326), JSSC 38(3), 2003. The [author-hosted full paper](https://personal.utdallas.edu/~hoilee/publications/Journals/affc.pdf), including the original main schematic and replica-bias circuit in Figs. 6–7, was reviewed. Active capacitive feedback provides a faster feedback path while feedforward action supports the output stage. The release includes both compensation capacitors and an auxiliary `gma` device group. Capacitors, reference bias, and main-stage multiplicities are reasonable sizing controls; the auxiliary feedback device is a separate physical control, not synonymous with `gm2`.

The source used 0.8 µm CMOS, a 2 V supply and 120 pF load. Its compensation values and stimulus are not all the released values: the release has 18 pF and 3 pF compensation capacitors and a 100 pF load. The original analytical explanation supports the topology choice, while feasibility of every new requirement comes from the actual project evaluator.

### Peng ACBC

Peng and Sansen, [AC Boosting Compensation Scheme for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2004.835811), JSSC 39(11), 2004. The [original six-page text](https://picture.iczhiku.com/resource/eetop/sHItHoErkwWsfMBC.pdf) explains a dedicated AC signal path parallel to the DC amplification path of the second stage. In the release, `CAPACITOR_0` is the outer Miller capacitor (`net050`–`VOUT`); `CAPACITOR_1` couples `net049` to `net1` in the AC branch. The auxiliary `gm4` device is part of that branch. Original implementation used 0.35 µm CMOS, a 2 V supply and 500 pF load; the released load is 100 pF. Shared nominal capacitor values do not make these equivalent experiments.

### Peng IAC and physically justified search bounds

Peng et al., [Impedance Adapting Compensation for Low-Power Multistage Amplifiers](https://doi.org/10.1109/JSSC.2010.2090088), JSSC 46(2), 2011. An original copy linked by the [coauthor laboratory bibliography](https://sites.google.com/view/impact-lab/publications) was downloaded and rendered. Original Fig. 3 and equations on pp. 446–448 were checked. `Cm=CAPACITOR_0`; `Ca=CAPACITOR_1`; `Ra=RESISTOR_0`. Unlike NMC, the series RC loads the intermediate node without placing a second Miller capacitor across the final stage. Original conditions were 0.35 µm, 1.5 V, 150 pF, Cm=0.5 pF, Ca=1.1 pF and Ra=750 kΩ.

The approximate model gives ωu≈gm1/Cm, |pnd2|≈gm2·Ra·gm3/CL, and |pnd3|≈1/(Ra·C2). Thus increasing Cm above a narrow multiple of its released value is physically justified when the adapted circuit has inadequate phase margin. Exposing Ra is also justified, although raising it improves one pole while lowering another. These relations presume sufficient stage gain, separated poles and compensation capacitances exceeding parasitics; they are guidance, not verification. The `gmt`-labelled transistor corresponds to a cascode/current-source load and is not the preferred first stability control. Fixed 60° quality criteria should be retained while experimentally testing broader Cm/Ra ranges.

### Ramos PFC

Ramos and Steyaert, [Three Stage Amplifier With Positive Feedback Compensation Scheme](https://doi.org/10.1109/CICC.2002.1012833), CICC 2002, pp. 333–336. The [original four-page transcript](https://www.scribd.com/document/241079749/01012833-Technical-paper) describes local positive feedback across the second stage to control damping, with feedforward action and outer compensation. The release matches this mechanism through `CAPACITOR_1` (`net050`–`net049`) and outer `CAPACITOR_0` (`net050`–`VOUT`). Increasing the positive-feedback capacitor cannot be assumed to monotonically improve stability.

Original conditions were 0.35 µm CMOS, ±0.75 V rails and 130 pF in parallel with 24 kΩ. Its measured 52° phase margin should not become the benchmark's stability floor; an adapted design may be required to meet 60° if that requirement is independently shown feasible. OCR errors prevent quantitative reliance on extracted equations or the transcript's malformed power unit. The 2004 journal sequel is a separate publication and must not silently replace the cited 2002 paper.

### Song DACFC: explicitly limited original-source review

Guo and Lee, [Dual Active-Capacitive-Feedback Compensation for Low-Power Large-Capacitive-Load Three-Stage Amplifiers](https://doi.org/10.1109/JSSC.2010.2092994), JSSC 46(2), 2011, pp. 452–464. Authorship and publication are confirmed by [Hoi Lee's bibliography](https://personal.utdallas.edu/~hoilee/publication.html). Original abstract-level evidence describes two active feedback paths, two beneficial LHP zeros and push-pull second/output stages. The released schematic and netlist contain two compensation capacitors, auxiliary `gma1`/`gma2` PMOS groups and an additional `gmf1` input pair. One capacitor is connected from `net4` to ground; counting only output-connected capacitors would miss the second feedback branch.

The original full paper and original schematic remain unverified in this review. The 2007 conference precursor has different reported capacitance and performance and is not interchangeable with the 2011 article. Section 4.3 of Grasso et al.'s [2022 analytical research](https://doi.org/10.1002/cta.3244) independently studies DACFC current-buffer paths and two LHP zeros, supporting the mechanism description. It does not substitute for a transistor-level comparison against the original 2011 figure. Keep this limitation visible if DACFC remains in the final release.

## Parameter and testbench rules inferred from the released artifacts

The authoritative upstream files are under [`external/AnalogGym/AnalogGym/Amplifier`](../external/AnalogGym/AnalogGym/Amplifier): `spice_netlist/`, `design_variables/`, and `schematic/`. All 16 nonempty released ngspice subcircuits inspected use the common pins `gnda vdda vinn vinp vout` and SKY130 `nfet_01v8`/`pfet_01v8` devices. Their geometry parameters are in micrometers under the model setup; capacitors, resistors and current sources use SPICE units. Exact control names, values and passive connections are in the JSON companion.

The `M` controls are integer device multiplicities. Their names include descriptive counts, but actual instance expressions can contain additional factors such as `4*M` or `8*M`. Use the released expression exactly. Preserve tied pairs and bias mirrors. Changing an input-pair multiplicity changes effective width, operating point and capacitance together; a parameter labelled `gm1` is not an ideal transconductance source. The common reference-current control changes multiple branches through fixed mirror ratios.

The upstream variable files provide `CLOAD` and `VCM`, but those values are not automatically enforced by a generic testbench. The upstream ACDC example uses 500 pF and a supply-relative input common mode, which disagree with the selected HoiLee variable file. Its transient example also contains an apparent duplicated `t_fall` measurement and a reference to `t_fall_`. Use the project's reviewed evaluator and explicit immutable conditions, not unexamined upstream `.meas` expressions.

For the proposed tasks, freeze the supply, temperature, model corner, load, input common mode, feedback configuration, edge rate, excursion, transient window and measurement definitions in each verifier. A 0.2 V follower step is a project choice, even where an original experiment used a larger swing or a resistive load. Nominal AC phase margin plus a specified step response does not imply rail-to-rail, output-current, noise, offset, distortion or all-load stability performance.

## Attribution corrections and omissions

- The [AnalogGym paper](https://arxiv.org/html/2409.08534) Table 6 appears to exchange the references attached to `Peng_ACBC` and `Peng_IAC`. The title/circuit mapping above uses ACBC→2004 DOI `10.1109/JSSC.2004.835811` and IAC→2011 DOI `10.1109/JSSC.2010.2090088`. Table 6's reported 180 nm numbers are not targets for SKY130.
- `Qu_LEC_Pin_3` has a zero-byte `spice_netlist` file. `Cascode_Miller_Pin_2`, `Cascode_Null_Pin_1`, `Davide_ASMIHF_Pin_3` and `TwoSt_SMCNR_Pin_2` have design-variable entries without corresponding runnable ngspice subcircuits in this checkout. Do not count names as available circuits.
- `Peng_TCFC_Pin_3` has a PMOS-input netlist while the available schematic is named `Peng_TCFC_Nin_3` and depicts NMOS inputs. Exact correspondence is unresolved; it is a reserve candidate.
- `Sau_CFCC_Pin_3` and `Qu2017_AZC_Pin_3` have released netlists but incomplete original-paper review in this pass. They are reserve candidates. Sau additionally lacked stable exploratory designs at the time of selection; that is a search result, not proof of impossibility.
- `Leung_NMCF_Pin_3` has a documented original topology but failed the initial stability screen. `Alfio_RAFFC_Pin_3` failed usable unity-crossing/DC operating-point screening; it should not be described as an AC-source syntax failure. `Tan_CLIA_Pin_3` and `Yan_AZ_Pin_3` require simulator/testbench investigation. Do not publish tasks with unverified or broken reference solutions to reach a round count.
- The JSON companion is a research shortlist. It deliberately records rejected/provisional candidates and may differ from the final empirically validated core. Every final task needs an actual passing private parameter set and reproducible evidence; original-paper prestige supplies neither.

## Local review evidence

Original figures were rendered with Poppler and visually inspected. Local working files include `autockt_fig6.png`, `leung_compensation_fig7.png`, `lee_mok_affc_fig6.png`, `peng_iac_fig3.png`, `peng_iac_equations_446.png`, and `peng_iac_equations_447.png` under `runs/research_sources_20260910/`. The JSON records original PDF hashes where downloaded. The released schematics were also compared with their corresponding netlists for the selected candidates. Figure correspondence is limited to the evidence levels stated above; no layout or extracted-netlist review was performed.
