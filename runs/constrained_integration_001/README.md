# TPU probability check 001

Stopped after the first generated response because the maximum sampler/teacher log-probability difference exceeded the predeclared 0.1 threshold. No evaluator attempts or optimizer updates followed. Mean error was within its separate 0.01 limit.

See [results](results.json), [plan](plan.json) and raw rollout logs. Run 002 includes the float32 temperature-division fix; the residual maximum remained about 0.257, so temperature rounding alone did not explain it. A subsequent float32 compute diagnostic is recorded separately. Source snapshots preserve the code loaded by each process.
