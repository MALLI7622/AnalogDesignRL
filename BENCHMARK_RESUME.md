# Resume the 250-task analog benchmark

Latest status: **completed on 2026-09-11**. Exactly **250 tasks: 100 easy,
125 medium, and 25 hard**. All fresh task and reference checks passed with
zero failures. All processes have exited; nothing needs resuming or rerunning.

## Finished artifacts

- [Dataset card](datasets/analog_benchmark_250_v1/README.md)
- [Task catalog](datasets/analog_benchmark_250_v1/TASK_CATALOG.md)
- [All 250 verified solutions](datasets/analog_benchmark_250_v1/private/README.md)
- [Public learner export](datasets/analog_benchmark_250_v1_learner/README.md)
- [Curation and source limitations](datasets/analog_benchmark_250_v1/CURATION_NOTES.md)
- [Final machine-readable audit](datasets/analog_benchmark_250_v1/audit_summary.json)
- [Worker catalog](datasets/analog_benchmark_250_v1/worker_catalog.json)
- [Private evidence manifest](datasets/analog_benchmark_250_v1/private/evidence/manifest.json)

There are 250 distinct requirement groups and reference vectors across ten
amplifier structures from eight papers. Whole-family splits contain 174 train,
21 validation, and 55 test tasks. The original assembly index remains immutable;
its SHA256 is `4e4974f6f67d40bd233640e2407728b8fa28d1d5e29a8bcf2d854ef0e01e7f48`.
Its original awaiting-verification status is historical; `audit_summary.json`
records the final `automated_verification_passed` result for that exact index.

## Completed verification

- [Fresh release verification](runs/benchmark_release_verification_001/verification.json):
  `verified`, `publication_ready: true`, zero errors. All 760 uncached circuit
  evaluations completed: 250 valid failing starts, 250 passing references,
  250 passing refined references, and ten public defaults. Default designs
  solve zero selected tasks and have zero unresolved outcomes. References pass
  all eight requirements with matching extraction-path decisions at both
  nominal and refined simulation resolution.
- [Project verification](runs/benchmark_final_project_verification_001/verification.json):
  all 288 unit tests and all eight analytical, author-reference, convergence,
  boundary, pilot, and prior-solution regression checks passed.
- [Selected-subset overlap audit](runs/research_sources_20260910/supplemental_discovery_selected250_001.json):
  passed, `final_selection_proof: true`, zero violations. Includes all twelve
  complete trajectory reports and 80 additional actual discovery vectors.
  Maximum observed single-witness coverage is 39.29% within a topology;
  descriptive greedy cover counts range from 7 to 20. These are finite-pool
  observations, not a proof of an optimal minimum cover or universal hardness.
- Final independent artifact review passed: all250 solution notes reproduce
  from fresh evidence, all40 copied evidence files match their hashes, and
  all4,276 local Markdown links resolve.
- Independent selection and learner-packaging reviews passed. All 250 public
  tasks match the export whitelist; no private-reference or trace leakage was
  found. The training qualification file is unchanged, with zero approvals.

Fresh verification session42389/PID97816 and its temporary caffeinate56248
exited0. Assembly82159, project verification54159, and all earlier calibration
processes also exited0. There is no active simulation or cache writer.

## Retained construction evidence

The frozen master is `runs/benchmark_frontier_candidates_001`: 1,500 internal
starts for 500 requirement groups. These are selection inputs; the final
benchmark contains exactly250 tasks. Completed stages are:

- `runs/benchmark_frontier_screen_001`: 3,384 screening trials.
- `runs/benchmark_frontier_confirm10_001`: 335 panels / 13,400 trials.
- `runs/benchmark_easy_reserve_confirm10_001`: six panels / 240 trials.
- `runs/benchmark_hard_confirm20_001`: 30 panels / 2,400 trials.

All stages use the four declared search methods and a 30-call budget. Main and
reserve confirmation uses seeds100–109; hard confirmation uses100–119 and
replaces, rather than pools with, the ten-seed observation for each hard task.
Final eligible availability was102 easy/184 medium/30 hard; exactly100/125/25
were selected without relabeling. Plans, all twelve trajectory reports,
original774 prepared targets/curation, the supplemental80 audit/script, the
seven-task legacy comparison, source reviews, and verification summaries are
copied into private evidence with hashes. Raw measurements and waveforms remain
under their original `runs/` paths; the cache is
`runs/benchmark_measurements_final_v2`. Do not delete these as part of resuming.

Detailed history is in `benchmark/WORK_LOG.md`. No further task generation is
needed. Keep private solutions and search evidence inaccessible to the learner.
The trusted driver spends the initial evaluation using `episode.step({})`
before providing the observation and specification, leaving29 calls.

Difficulty is relative to the search baselines, not an evaluated LLM.
Confirmation statistics are curation results, not an independent post-selection
test. Scope is nominal circuit simulation, with no layout, noise, mismatch or
PVT qualification. The user requested task generation; model training has not
been launched and `verification/qualification.json` has not been changed.

## Historical pause and completed screening

The original screening pause inventory remains preserved at
`runs/benchmark_pause_20260910T224701Z/pause_manifest.json`: 1,207 trial
checkpoints and 18,363 committed physical cache entries were verified then.
Eight uncommitted measurement directories were preserved outside the active
cache. Original PID69997/session25620 exited before the user-authorized
restart in PID23760/session7393. Frozen inputs and completed checkpoint hashes
were verified before that restart. Screening then finished with exit 0 and
all 3,384 required trials; its temporary caffeinate session18514 also exited.
These pause counts are historical. Nothing was deleted.

The main ten-seed run session6259/PID45183 and its caffeinate39414 completed
with exit0. The six-reserve run session74593/PID52705 and caffeinate78571 also
completed with exit0. Do not restart either completed run or its old watcher.

## Paper download and review status

Eight original papers underpin the ten selected circuit structures. Four
original PDFs are present locally:

| Paper | Local PDF |
|---|---|
| AutoCkt (2020) | `external/AutoCkt-2001.01808v2.pdf` |
| Leung compensation analysis (2001; three structures) | `runs/research_sources_20260910/leung_compensation_2001.pdf` |
| Lee–Mok active feedback (2003) | `runs/research_sources_20260910/lee_mok_affc_2003.pdf` |
| Peng impedance adapting compensation (2011) | `runs/research_sources_20260910/peng_iac_2011.pdf` |

Fan SMC (2005) and Peng AC boosting (2004) were reviewed through online full-text
extraction; their original PDFs are not saved locally. Ramos positive feedback
(2002) was reviewed through an OCR transcript with formula/figure limitations.
The original Guo–Lee DACFC (2011; release name `Song_DACFC_Pin_3`) full paper has
not been recovered. Its evidence is the original abstract, author bibliography,
released circuit and supplementary analysis; preserve this limitation.

Additional research PDFs are separate from these eight source papers. See
`reports/benchmark_paper_sources.md` and
`benchmark/research/paper_candidates.json` for source links, review levels and
provenance. Research PDF copies are not redistribution assets for the dataset.

To continue in a later session, ask: **“Read BENCHMARK_RESUME.md and continue the
250-task benchmark from its saved checkpoints.”**
