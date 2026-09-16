# Analog sizing benchmark tooling

This directory builds a paper-grounded, simulator-verified curriculum from the
project's existing analog evaluator. The intended release has 250 tasks. The
source catalog and intermediate candidate directories are **not** themselves a
finished or training-approved benchmark. Consult the generated release's dataset
card and verification report for its actual status.

The current scope is transistor-level amplifier sizing at nominal SKY130 TT,
1.8 V and 27 °C. Ten circuit structures come from eight publications, with the
three related Leung circuits grouped into one source family. Published circuit
structures inform the experiments; project-generated requirements and adapted
technology do not reproduce the papers' reported silicon performance.

## Evidence and outputs

- `domains.json`: circuit conditions, permitted controls, source attribution,
  and the fixed minimum requirements for each domain.
- `DESIGN_BRIEFS.md`: descriptions of the ten structures and their editable
  controls, with source-review limits and no passing parameter vectors.
- `../reports/benchmark_paper_sources.md` and
  `../reports/benchmark_additional_sources.md`: original-paper review, circuit
  adaptations, unavailable evidence, and excluded source candidates.
- `../reports/benchmark_protocol_review.md`: measurement and benchmark protocol.
- `../reports/benchmark_candidate_sample_review.md`: review of concrete early
  candidates, including solution-overlap problems found before final selection.
- `../reports/benchmark_final_candidate_review.md`: review of the frozen joint
  targets, including twelve examples spanning all ten structures. This contains
  evaluator-side evidence and must stay outside the learner export.
- `THIRD_PARTY_NOTICES.md`: license and attribution for imported circuit assets.

A release separates executable tasks, private passing parameter vectors,
calibration evidence, and an answer-free learner export. Keep private references,
source parameter files, characterization data, and baseline trajectories outside
the learner's accessible filesystem. Export filtering alone is not a sandbox.

## Build stages

1. Review the original paper and the exact released circuit. Preserve source
   versions, topology limitations, units and adaptation decisions.
2. Characterize legal parameter vectors with `benchmark.explore`. Record failed
   simulations as well as valid measurements. A passing measurement is a witness
   to feasibility under the specified model, not an optimum or a silicon claim.
3. Construct candidate requirements and valid failing starts. Recheck rounded
   values, reject targets already satisfied by the released default, and inspect
   meaningful tradeoffs and reference-versus-task coverage. Distinct filenames
   or parameter hashes do not establish distinct optimization problems.
4. Run `benchmark.calibrate` with all four bounded search methods. Use separate
   output directories for the screening and confirmation seed panels. Inputs,
   simulator/model dependencies and calibration code are bound to checkpoint
   hashes; changing them requires a new run.
5. Audit completed search trajectories with
   `benchmark.research.trajectory_overlap`, then supply the resulting complete
   reports to `benchmark.frontier_select --trajectory-coverage`. Select using
   confirmed observations and overlap across the original measured pool and
   designs discovered by the solvers. The overlap filter conservatively counts
   either measurement path passing; this is potential shared-answer coverage,
   not confirmed feasibility in both paths. Construction hints never determine
   final difficulty.
6. Assemble with `benchmark.release`, then independently run
   `scripts/verify_benchmark.py --simulate` on the resulting index. Fresh starts,
   passing witnesses, finer-resolution witness simulations, public-default
   replay and cross-task solution coverage belong to this final check.
7. Export learner specifications with `benchmark.learner`, and generate the
   dataset card, task catalog and audit summary from actual artifacts.

The four baselines are uniform linear sampling, uniform logarithmic sampling,
coordinate search, and an engineering sweep of component-value factors with
feedback-guided combinations and refinement. All consume the same 30-evaluation
budget. Evaluation 1 checks
the supplied initial design; invalid actions and failed simulations consume an
evaluation. Random methods sample the full declared domain; coordinate search
uses numeric feedback and never receives a passing reference.

The difficulty convention is relative to this method panel. An easy task has a
method with at least 80% success by evaluation 30 and a censored median of at most
10 calls. Hard requires every method to have at most 20% success over at least
20 seeds each. Other fully measured tasks are medium. Easy and medium require at
least 10 confirmation seeds per method. Report success counts and confidence
intervals alongside these labels; the labels do not predict an untested LLM's
performance or establish universal optimization hardness.

These seed panels support task curation. Their reported success rates are
calibration statistics, not an independent test score after dataset selection.
Evaluate compared solvers again on fresh seeds once the release is frozen.
The project's current Gemma training example uses a smaller episode-attempt cap;
comparison with this benchmark's baselines requires the same 30-evaluation
convention. No untrained Gemma performance is inferred from these searches.

Creating an `Episode` or `BenchmarkEpisode` does not evaluate the starting design.
For comparison with these baselines, the trusted driver must call
`episode.step({})` once before the learner proposes changes, give the learner that
observation and `episode.specification()`, and allow the remaining 29 evaluations.
The current worker catalog adapter does not add this initial call.

## Entry points

Each build command has `--help`, for example:

```bash
python3 -m benchmark.explore --help
python3 -m benchmark.frontier_candidates --help
python3 -m benchmark.calibrate --help
python3 -m benchmark.research.trajectory_overlap --help
python3 -m benchmark.frontier_select --help
python3 -m benchmark.release --help
python3 scripts/verify_benchmark.py --help
python3 -m benchmark.solution_notes --help
python3 -m benchmark.worker_catalog --help
```

Use `benchmark.learner.BenchmarkEpisode` in the trusted evaluator process to
include the symbolic netlist and permitted-control context in the learner's
observation. Its constructor preserves the existing `for_training=True`
qualification gate. Benchmark generation and automated verification do not add
reviewer approval or start a model-training run.

After a fresh release verification passes, `benchmark.solution_notes` creates
private per-task explanations of the supplied feasible witness, including the
starting values, reference values, measured requirements and numerical slack.
These notes are answers and must stay outside the learner export.
`benchmark.worker_catalog` adapts the release index to the existing numeric
worker's catalog format while preserving family splits. The current worker uses
the base `Episode`; this export does not activate `BenchmarkEpisode`'s symbolic
circuit context or grant training approval.

The measurement cache avoids repeated physical simulations for identical
physics and parameter vectors, while every logical solver call remains charged
to its episode. Cache reuse is keyed by the model include closure, simulator
executable, evaluator code and numerical settings. Threads in one cache process
are coordinated; do not concurrently populate the same cache from independent
processes.

## Interpretation limits

The benchmark measures gain, unity-gain frequency, phase margin, quiescent power,
DC error, steady tracking error, and rising/falling settling under the declared
tests. The nine AnalogGym-derived structures use a 200 mV follower step and a
20 mV settling band (10%); AutoCkt uses 100 mV and 2 mV (2%). Steady error limits
are separate from the settling band.

There is no qualification here for layout, mismatch, noise, distortion, CMRR,
PSRR, process corners, temperature sweeps or arbitrary load ranges. Split
assignment holds out complete source families; related circuits within a family
must not be split across training and evaluation. A useful trained model still
requires a measured training experiment and evaluation on those held-out tasks.
