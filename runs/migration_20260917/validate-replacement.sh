#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
source training/activate.sh
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
python -m training.worker \
  --catalog datasets/analog_benchmark_250_v1/train_validation_catalog.json \
  --output runs/migration_20260917/worker --mode evaluation --port 18765 --workers 1 \
  > runs/migration_20260917/worker.log 2>&1 &
ANALOG_MIGRATION_WORKER_PID=$!
trap 'kill "$ANALOG_MIGRATION_WORKER_PID" 2>/dev/null || true; wait "$ANALOG_MIGRATION_WORKER_PID" 2>/dev/null || true' EXIT
python - <<'PY'
import os,time
from training.client import WorkerClient
client=WorkerClient('http://127.0.0.1:18765',os.environ['ANALOG_WORKER_TOKEN'])
for attempt in range(30):
    try:
        client.request('/catalog')
        break
    except Exception:
        if attempt==29: raise
        time.sleep(1)
PY
timeout 600 python -m training.train --mode rollout \
  --config runs/migration_20260917/validation-config.json \
  --split validation --max-tasks 1 --episodes-per-task 1 \
  --restore-checkpoint /home/cheriearjun/AnalogDesignRL/runs/research_pilot_007/train/checkpoints \
  --restore-run /home/cheriearjun/AnalogDesignRL/runs/research_pilot_007/train/run.json \
  --output runs/migration_20260917/checkpoint_rollout \
  > runs/migration_20260917/checkpoint-rollout.log 2>&1
python - <<'PY'
import hashlib,json
from pathlib import Path
root=Path('.')
manifest=json.loads(Path('runs/migration_20260917/source-manifest.json').read_text())
for name,record in manifest['files'].items():
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=record['sha256']:
        raise SystemExit('Source provenance mismatch: '+name)
p=Path('runs/migration_20260917/checkpoint_rollout/rollouts.jsonl')
rows=[json.loads(line) for line in p.read_text().splitlines()]
if len(rows)!=1:
    raise SystemExit('Expected one completed model/evaluator interaction')
Path('runs/migration_20260917/validation-complete.json').write_text(json.dumps({
    'status':'passed','scope':'environment, source provenance, checkpoint restoration and one model/evaluator interaction; no optimizer updates',
    'rollout_rows':len(rows),'simulator_invocations':rows[0].get('simulator_invocations'),
    'circuit_solved':rows[0].get('success'),'verifier_reward':rows[0].get('verifier_reward'),
},indent=2)+'\n')
PY
