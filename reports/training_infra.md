# Training infrastructure verification — 2026-09-10

**CPU integration passed. TPU execution is not tested. No model was trained.**

- All 80 project unit tests passed, including HTTP authentication, task/split integrity, numeric-format duplicate grouping, invalid-action budgets, idempotent/concurrent retries, final-score aggregation, infrastructure errors, and training-approval enforcement.
- The real HTTP smoke test completed two concurrent episodes, four candidate evaluations, and eight ngspice invocations. Both episodes reached their known public passing fixtures; each episode's summed training-adapter reward was +1.
- Replaying a completed step did not invoke the simulator again or spend another evaluation.
- Mean candidate evaluation time was 1.247 seconds; total wall time was 3.334 seconds. The run produced 3,554,535 bytes of artifacts. This small macOS sample is not a TPU-host performance estimate.
- Python files compile and the Ubuntu bootstrap passes shell syntax checking. Trainer calls were checked against Tunix v0.1.7 source, commit `ce63a9fd65f02c4c398e74f000f98371c1575eb6`. This does not establish runtime compatibility.

Commands used:

```sh
python3 -m unittest discover -s tests -q
python3 -m training.smoke --output runs/training_infra_smoke_20260910_retry
python3 -m compileall -q training
bash -n training/bootstrap.sh
python3 -m training.train --mode plan
python3 -m training.cloud_plan
```

The local sandbox initially rejected socket binding. The unit suite and real smoke test were then run with permission to open localhost listeners. The first empty smoke directory is retained separately.

Machine-readable results: [training_infra.json](training_infra.json). Raw artifacts: `runs/training_infra_smoke_20260910_retry/`. Public passing fixtures are used only by this diagnostic, not included in learner inputs.

Still to validate on the allocated VM: Ubuntu package installation and the resolved dependency set; model access/tokenization; TPU memory and generation; finite loss, changed LoRA weights, and feedback token masks in an optimizer step; checkpoint save/restore and interruption handling. Dataset review, frozen splits, matched baselines, and sufficient task diversity remain separate requirements for a learning experiment. The current trainer implements new runs, not checkpoint resume.
