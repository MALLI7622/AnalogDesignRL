# Learner-facing circuit context review

Reviewed 2026-09-10 against `benchmark/domains.json` SHA256 `0de38857961a21b04a14f8d2dc43849f0b954b198a074ff07e57ba8ba93f1e87`. All ten domain contexts were checked against their copied circuit netlists, exact editable parameter bindings, prior paper/schematic review records, and the metric extraction and learner-export text. This review did not change the catalog, source files, physical task fields, active candidates, evaluator, frontier generator, or start characterization code.

The contexts provide the actual connections and fixed geometry needed for circuit reasoning. No newly selected passing vector or reference-result artifact is included in the reviewed `design_context` objects. `fixed_parameters` excludes every editable parameter in all ten domains. Released or pilot values elsewhere in the repository are not private solution isolation: the learner export remains the intended boundary, and filesystem isolation remains an integration responsibility.

The main remaining issue is explanatory precision. Generic capacitor labels obscure distinct feedback paths. A DFC-specific caveat has also been copied into the NMCNR description. The descriptions are safe to improve at a metadata version boundary before final candidate construction and calibration; changing them during a frozen run would change the input presented to the learner.

## Concrete corrections and clarifications

The exact proposed JSON-pointer field paths, old strings, and replacement strings are in [benchmark_public_context_edits.json](benchmark_public_context_edits.json). It contains **47 proposed, unapplied edits**: two circuit-description corrections/clarifications, eighteen passive-control role clarifications, and twenty-seven optional transistor-role clarifications. Every old string was matched against the catalog snapshot above. No proposal changes parameter bounds, numeric values, connections, test conditions, constraints, source records, or measurement code.

The two description edits are:

| Field | Exact change | Why |
|---|---|---|
| `/entries/2/task/design_context/circuit_description` (`Leung_NMCNR_Pin_3`) | Replace the ending `; DFC high-impedance DC node robustness is not established by nominal sizing.` with `.` | That paper discussion concerns DFC variants. Applying it to the distinct nested-Miller/nulling-resistor topology misattributes the limitation. |
| `/entries/9/task/design_context/circuit_description` (`Song_DACFC_Pin_3`) | Append ` These circuit roles follow the released AnalogGym schematic and netlist. Original-paper review was limited to the abstract and author bibliography; the original full text and original schematic were unavailable.` | The source limitation is recorded in research metadata but is absent from the public paper citation/context. This preserves the distinction between reviewed release artifacts and unavailable original full text. |

The capacitor and resistor roles should name these exact physical connections. Their complete old/new strings and paths are in the proposed-edit JSON:

| Domain | Control | Physical role to expose |
|---|---|---|
| Fan | `CAPACITOR_0` | Outer Miller branch, `net050–VOUT`. |
| NMCNR | `CAPACITOR_0`, `CAPACITOR_1` | Outer `net050–net044` and inner `net049–net044` branches sharing the fixed resistor return to `VOUT`. The resistor is fixed in this domain. |
| DFCFC1 | `CAPACITOR_0`, `CAPACITOR_1` | Outer `net050–VOUT` and local `net049–net1` damping-control branch across auxiliary NMOS `xm24`. |
| DFCFC2 | `CAPACITOR_0`, `CAPACITOR_1` | Outer `net050–VOUT` in instance `C1`, local `net050–net2` across auxiliary PMOS `xm10` in instance `C2`. Parameter and instance suffixes differ. |
| Hoi Lee AFFC | `CAPACITOR_0`, `CAPACITOR_1` | Inner output-stage branch `net049–VOUT`, active-feedback branch `VOUT–net1` into common-gate NMOS `xm63`'s source. CAP0 is not an outer first-stage-to-output Miller capacitor here. |
| ACBC | `CAPACITOR_0`, `CAPACITOR_1` | Outer `net050–VOUT`, AC-boosting `net049–net1`. Auxiliary NMOS `xm24` is driven by `net043`, unlike DFCFC1's `net049`. |
| IAC | `CAPACITOR_0`, `CAPACITOR_1`, `RESISTOR_0` | Outer Cm `VOUTP–VOUT`, Ca `net10–net4`, Ra `net4–ground`. Ca and Ra form a series branch from intermediate output `net10` to ground. |
| Ramos | `CAPACITOR_0`, `CAPACITOR_1` | Outer `net050–VOUT`, local positive-feedback branch `net050–net049`. A blanket “increase capacitance for stability” description would be misleading. |
| Song | `CAPACITOR_0`, `CAPACITOR_1` | Feedback branch `VOUT–net70`, first-stage shunt `net4–ground` in instance `C2`. CAP1 is not an output-spanning capacitor. |

The existing `gm1/gm2/gm3 stage transistor multiplicity` wording is not false, but it leaves an avoidable ambiguity for learners unfamiliar with the release names. The twenty-seven optional role replacements make the control quantities explicit: shared integer multiplicity of the matched PMOS input pair, integer multiplicity of the PMOS intermediate-stage signal device, and integer multiplicity of the NMOS output gain device. Width and length remain fixed for each individual device. These controls change operating point and parasitics together with transconductance. They are not ideal independent `gm` values.

AutoCkt's six width, bias, and Miller-capacitor labels already identify the physical controls correctly. Its shared widths preserve matching between the two input devices and between the first-stage mirror devices. No role edits are proposed there.

## Task purpose and measurement wording

The nine AnalogGym domain-template `purpose` strings still describe a particular larger-capacitance starting perturbation. The AutoCkt purpose still describes the narrow pilot exercise. Those descriptions are stale for a general frontier-derived start. The root workflow already plans to assign purpose text during frontier candidate creation. A suitable topology-independent statement is: **“Size the allowed device groups and compensation elements to satisfy all specified gain, bandwidth, stability, power, tracking, and settling requirements under the fixed test conditions.”** It does not assert how the starting point was made or which edit will solve it. The purpose is not changed by this review.

`benchmark/learner.py`'s export README already correctly describes quiescent power, error in steady waveform portions, gain at the start of the AC sweep, the single downward crossing, and the separate settling band. No measurement-definition edit is required. The standalone briefs add precision that can help human readers:

- `max_tracking_error_v` samples the pre-rise interval and final quarter of each observed plateau. It is not peak error across the full waveform.
- Settling time is measured from the corresponding edge start, to the first accepted sample after the last out-of-band sample in that observation window. At least `minimum_hold_s` must remain, and the sampled response stays in band through the rest of the window. This is stronger than merely entering the band once.
- `dc_error_v` is a single nominal follower operating-point error, not a mismatch-derived input-offset specification.
- Phase margin is extracted from the stated nominal AC test, with missing/multiple unity crossings rejected. No all-corner, all-load, or global nonlinear-stability guarantee follows.
- `power_w` is quiescent DC supply power. Low-power Fan or AutoCkt exercises should not be described as minimizing switching energy or average transient power.
- Each task states its own absolute transient settling tolerance. A common verbal “2% settling” label would be wrong across domains because AutoCkt and the AnalogGym tests use different step/band combinations.

No qualitative brief should promise that a given direction of a knob always improves a metric. This is particularly consequential for IAC's coupled Ca/Ra pole movement, Ramos's positive-feedback capacitor, and any output-device multiplicity that simultaneously changes output drive and gate loading of the previous stage.

## Standalone deliverables and validation

[benchmark/design_briefs.json](../benchmark/design_briefs.json) provides a short natural design brief for each topology, **all sixty editable controls**, exact bound instance names and terminals, units, netlist SHA256, fixed-structure notes, source citation, and explicit paper-review scope. [benchmark/DESIGN_BRIEFS.md](../benchmark/DESIGN_BRIEFS.md) presents the same material as a human-readable educational guide. Neither file contains numerical passing vectors, requirements generated from a reference, simulation traces, private reference identifiers, or suggested parameter updates.

The briefs distinguish the full original-paper/schematic reviews of AutoCkt, Leung, Hoi Lee, and IAC from original text plus released-schematic review for Fan and ACBC, OCR/transcript plus released-schematic review for Ramos, and the limited original abstract/bibliography review for Song. Circuit-role claims for Song rely on the inspected released netlist and schematic. None of these descriptions claims reproduction of original-paper performance after SKY130 substitution.

Validation completed: all ten circuit files exactly match their embedded context netlists; all sixty controls have exact corresponding instance bindings and matching units; each recorded netlist hash matches its file; all editable names are absent from `fixed_parameters`; proposed edit paths resolve and every old string matches; the reviewed catalog hash is unchanged. The new educational JSON was checked for absence of answer-bearing fields. No extra simulation or calibration was run for this documentation-only review.

The new files are not imported by runtime code and do not change an active learner context. If the briefs or proposed strings are integrated later, rebuild the affected task/context hashes and use a calibration manifest for that version. Preserve historical source records unless a concrete factual correction is separately warranted.
