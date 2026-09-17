#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
umask 077
source training/activate.sh
python -m pip check
# Numerical unit tests in this project explicitly target CPU. Check TPU
# availability separately and run the real checkpoint rollout on TPU.
JAX_PLATFORMS=cpu python scripts/verify.py > runs/migration_20260917/verification.log 2>&1
JAX_PLATFORMS=cpu python -m training.smoke > runs/migration_20260917/smoke.log 2>&1
python -m training.preflight --tpu > runs/migration_20260917/tpu.json
python -m training.preflight > runs/migration_20260917/simulator.json
python -m pip freeze > runs/migration_20260917/requirements-resolved-new.txt
python - <<'PY'
import os,secrets,json
from pathlib import Path
p=Path('.cache/worker.token')
if not p.exists():
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(secrets.token_urlsafe(32))
record=json.loads(Path('runs/migration_20260917/verification.log').read_text())
assert record['automated_checks_passed'], record
report=Path(record['report'])
Path('runs/migration_20260917/tests.log').write_text((report.parent/'unit_tests.txt').read_text())
old=set(Path('runs/environment/requirements-resolved.txt').read_text().splitlines())
new=set(Path('runs/migration_20260917/requirements-resolved-new.txt').read_text().splitlines())
if old != new:
    raise SystemExit('Installed dependency freeze differs: '+repr({'removed':sorted(old-new),'added':sorted(new-old)}))
PY
date -u +%FT%TZ > runs/migration_20260917/bootstrap-complete.txt
