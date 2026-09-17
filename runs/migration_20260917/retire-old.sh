#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
python3 - <<'PY'
import json
from pathlib import Path
p=Path('runs/migration_20260917')
assert json.loads((p/'validation-complete.json').read_text())['status']=='passed'
assert (p/'backup-complete.txt').read_text().strip()
PY
test "$(gcloud alpha compute tpus tpu-vm describe analog-rl-v5e-20260917-24h --project=interpretable-ml-moleculelens --zone=us-west4-a --format='value(state)')" = READY
gcloud alpha compute tpus queued-resources delete analog-rl-v5e-request-20260916 \
  --project=interpretable-ml-moleculelens --zone=us-west4-a \
  --force --async --quiet > runs/migration_20260917/old-retirement-request.log 2>&1
date -u +%FT%TZ > runs/migration_20260917/old-retirement-requested.txt
cat runs/migration_20260917/old-retirement-request.log
printf '%s\n' 'Old TPU deletion request accepted.'
gcloud storage cp runs/migration_20260917/old-retirement-request.log \
  runs/migration_20260917/old-retirement-requested.txt \
  gs://molecule-lens/analog-design/runs/migration_20260917/
