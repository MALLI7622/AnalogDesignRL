#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
umask 077
mkdir -p runs/migration_20260917
date -u +%FT%TZ > runs/migration_20260917/bootstrap-started.txt
bash training/bootstrap.sh > runs/migration_20260917/bootstrap.log 2>&1
source training/activate.sh
python -m unittest discover -s tests > runs/migration_20260917/tests.log 2>&1
python -m training.preflight --tpu > runs/migration_20260917/tpu.json
python -m training.preflight > runs/migration_20260917/simulator.json
python - <<'PY'
import os,secrets
from pathlib import Path
p=Path('.cache/worker.token')
if not p.exists():
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(secrets.token_urlsafe(32))
PY
date -u +%FT%TZ > runs/migration_20260917/bootstrap-complete.txt
