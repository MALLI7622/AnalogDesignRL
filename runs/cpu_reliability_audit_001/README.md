# CPU reliability audit — 2026-09-17

Completed without model inference, weight loading, training, or TPU computation. This establishes software correctness for the checks below; improved model behavior remains untested.

## Findings and repairs

The inspected episode repeated its starting values in the raw model replies. The evaluator did not discard a changed proposal. Separately, the old chat serializer omitted BOS and completed-assistant end-of-turn markers and could mutate conversation history.

`training/chat_format.py` now uses the pinned tokenizer template and preserves history. Incremental training context restores the stop token excluded by the pinned Tunix sampler. A startup check runs before explicit TPU initialization and weight loading. Actual completion-token alignment is recorded for every generation; training rejects mismatches before optimizer use. Future research manifests also bind the formatter source.

## Evidence

- [Final chat replay](chat_replay_final.json): all 671 recorded prompts from 168 episodes reproduced under the old serializer; all 671 corrected prompts match the pinned reference template. Maximum corrected recorded prompt length: 5,871 tokens. The ungenerated overflow prompt is not included, so this does not establish that context overflow is fixed.
- [Parameter application audit](parameter_application/report.json): four diagnostic HTTP actions, six actual ngspice invocations. Bias and capacitor edits reached the written SPICE parameters, omitted values retained prior edits, and an invalid key changed no state and triggered no simulation. Unity-gain frequency changed from 1,366,558 Hz to 1,350,537 Hz to 1,317,026 Hz. These deliberately chosen edits test application, not optimization.
- [Suite log](tests.log): 60 tests passed. [Alignment guard tests](alignment_guard_tests.log): eight passed, including one newly added test; 61 distinct tests passed in total. Tests include the installed Tunix trajectory collector with synthetic completions, token/loss-mask agreement, history preservation, and rejection with raw-response retention.

`chat_replay.json` is superseded by `chat_replay_final.json`: the final audit models the sampler's actual exclusion of the stop token. Neither replay generated model responses.

## Proposed next experiment — not launched

[Draft limits and criteria](proposed_next_run.json) and [draft config](proposed_smoke_config.json): unchanged current prompt and checkpoint, one task, two episodes, two attempts each. Maximum four model calls, eight ngspice invocations, and five minutes plus a 15-second termination grace; no automatic retries or new resources. Check valid JSON, exact token alignment, and at least one accepted parameter change. Copy-only behavior is a failed result: stop and inspect. Passing only supports review of the trajectories, not automatic expansion or training. These are proposed limits, not evidence of an executed or enforced run.

## Resource status

The read-only [resource check](resource_status.json) at 04:28 UTC found `analog-rl-v5e-request-20260916` ACTIVE, with recorded termination at 06:53:09 UTC. CPU-only work does not stop idle allocation charges. No cloud resource was changed. Preserve boot-disk artifacts before any separately authorized deletion or expiry.
