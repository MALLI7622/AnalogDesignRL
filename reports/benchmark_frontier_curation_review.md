# Joint-target curation remedy

Reviewed and implemented separately on 2026-09-10. Frozen `runs/benchmark_candidates_001` and its screening data were not modified. The representative review is in `reports/benchmark_candidate_sample_review.md`; all 12 selected examples subsequently had passing fresh admissions. This document concerns a proposed replacement candidate-construction method, not approved training tasks.

## Why selecting fewer old Fan tasks cannot fix the problem

One measured Fan design solves 48 of the original 50 candidate requirements. On any subset of n tasks it therefore solves at least n−2. Requiring maximum single-design coverage of at most 60% forces n≤5. After the first greedy solution, no more than two tasks remain, so greedy reference cover is at most three on any subset. The original Fan pool cannot satisfy both the requested 60% cap and a greedy cover of at least four. A full measured-pool audit also found a three-design exact cover of all 50 IAC candidates. These results favor changing target construction rather than describing filenames or distinct private reference vectors as sufficient diversity.

## Remedy and numerical contract

`benchmark/frontier_candidates.py` constructs a joint target for all eight existing metrics from each eligible measured design. It retains the existing circuit, bounds, model, supply, temperature, load, common mode and measurement definitions. Targets remain explicitly project-authored; none are claimed as recovered paper specifications.

Targets use 1 dB and 1° grids for gain and phase margin. Other metrics use the preferred-number sequence `1, 1.2, 1.5, 1.8, 2, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2` times powers of ten. For a minimum requirement, the target must be at most `measurement/1.02`; for a maximum requirement, it must be at least `measurement/0.98`. Absolute margins also apply: 0.2 dB gain, 0.3° phase, 10 Hz unity frequency, 10 nW power, 4 µV errors and 12 ns settling. Gain and phase use their absolute margins rather than an additional percentage. Rounding moves outward from these limits; template quality floors are retained.

The constructor requires measured public-default evidence, rejects targets already solved by that default, filters invalid measurements and parameter values, and preserves genuine count shortfalls. Selection enforces distinct measured feasible subsets and at least 0.025 normalized logarithmic parameter distance between references. It greedily balances how often every characterized design can solve the selected requirements. Coverage is computed against the whole valid measured pool, not merely the references attached to selected tasks.

## Prepared target artifact

The tested CLI produced **774 proposed targets** at `runs/research_sources_20260910/frontier_prepared_080/targets.json`. This is an object with a `targets` array. Each entry includes `topology`, `key`, rounded `parameters`, `constraints` and `measurement_directory`; it also records raw measurement parameters, `characterization_key`, source-derived measurements, feasible design keys and whether the measured design is Pareto-optimal within the measured eligible pool. The `characterization_key` preserves a join to earlier analysis artifacts using raw parameter hashes.

| Topology | Selected targets | Largest single measured-design coverage | Greedy cover |
|---|---:|---:|---:|
| AutoCkt | 73 | 24 / 73 | 31 |
| Fan | 80 | 9 / 80 | 37 |
| NMCNR | 75 | 13 / 75 | 41 |
| DFCFC1 | 66 | 9 / 66 | 38 |
| DFCFC2 | 80 | 2 / 80 | 69 |
| AFFC | 80 | 3 / 80 | 58 |
| ACBC | 80 | 2 / 80 | 66 |
| IAC | 80 | 3 / 80 | 55 |
| Ramos | 80 | 7 / 80 | 49 |
| Song | 80 | 1 / 80 | 80 |

All proposed per-topology sets satisfy the suggested measured-pool coverage criteria. The AutoCkt, NMCNR and DFCFC1 shortfalls relative to 80 are reported, not filled with duplicate tasks. No new operating conditions are necessary to address the observed overlap at this stage.

The earlier independent analysis at `runs/research_sources_20260910/frontier_remedy_summary.json` compared selection from all eligible designs with selection restricted to their measured Pareto frontier. It could select 50 references from either group in every topology except the then-available Pareto-only NMCNR group, which supplied 45. Fan had 95 distinct Pareto-derived tight feasible sets; 50 selected references had maximum measured coverage 3/50 and greedy cover 36. The permanent constructor additionally canonicalizes rounded vectors and the prepared artifact records its own exact pool and coverage counts. Counts from different construction snapshots should not be combined.

## Starts and qualification

`choose_targets(entry, designs, count=50)` returns `(targets, report)`. The CLI defaults to 80 requested targets per topology and supports `--targets-only`. `--starts` accepts source-validated exploration arrays tagged with `target_key`; tags are preserved when one measured start vector is associated with multiple targets. Candidate construction requires a valid failing start with at least one violation exceeding the same absolute/2% numerical margin. Near-boundary failures, passing starts, invalid measurements and a rounded start equal to its reference are excluded.

The emitted candidates use the existing schema, a balanced joint-requirement profile and easy/medium/hard construction hints. The hints determine which available start is chosen; they never constitute calibrated difficulty. One- or two-control physical perturbations are a reasonable way to obtain accessible starting exercises under the same tight requirements, provided the changed circuits are actually simulated and independent baseline trials determine the final labels. Very tight feasible sets alone are not a reason to call a task hard.

Eighteen targeted frontier tests pass, covering target-relative rounding, absolute settling margin, base floors, finite values, valid bounds and integer controls, rounded and missing defaults, public-seed wins, feasible-set/reference diversity, deterministic selection, count shortfalls, weak starts and preserved target tags. The 22 earlier curation tests also pass. These tests do not replace fresh reference/public-seed admission, refined simulation or the full baseline-method panel and required trial counts.

Coverage counts are descriptive and limited to the measured pool; unseen designs may solve more tasks. Some archived reference measurements precede four-significant-digit parameter rounding, which is flagged in each target and must be resolved through fresh admission. Song's original full-paper review limitation remains unchanged. The tight target construction adds no claims about PVT, noise, mismatch, layout or global optimality, and no training approval is created.
