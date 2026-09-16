#!/usr/bin/env bash
# Run on the Ubuntu TPU VM, from a clone on the retained data disk.
set -euo pipefail
test -f training/requirements-tpu.txt
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
source training/activate.sh

sudo apt-get update
sudo apt-get install -y python3-venv python3-pip build-essential bison flex libreadline-dev libfftw3-dev curl git pkg-config
if ! test -x .venv-bootstrap/bin/uv; then
  python3 -m venv .venv-bootstrap
  .venv-bootstrap/bin/python -m pip install uv
fi
.venv-bootstrap/bin/uv python install 3.12
ANALOG_NEW_ENV=false
if ! test -x .venv-tpu/bin/python; then
  .venv-bootstrap/bin/uv venv --python 3.12 .venv-tpu
  ANALOG_NEW_ENV=true
fi
if ! .venv-tpu/bin/python -m pip --version >/dev/null 2>&1; then
  .venv-bootstrap/bin/uv pip install --python .venv-tpu/bin/python pip
fi
mkdir -p runs/environment
ANALOG_REQUIREMENTS_SHA="$(.venv-tpu/bin/python -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("training/requirements-tpu.txt").read_bytes()).hexdigest())')"
if test -f runs/environment/requirements-input.sha256 && \
    test "$(cat runs/environment/requirements-input.sha256)" = "$ANALOG_REQUIREMENTS_SHA" && \
    test -s runs/environment/requirements-resolved.txt; then
  if test "$ANALOG_NEW_ENV" = true; then
    .venv-tpu/bin/python -m pip install -r runs/environment/requirements-resolved.txt
  else
    printf '%s\n' 'Reusing the existing Python environment; requirements are unchanged.'
  fi
else
  .venv-tpu/bin/python -m pip install -r training/requirements-tpu.txt
fi
.venv-tpu/bin/python -m pip check

# Same ngspice source release as the current verifier. Checksum from the locally
# installed Homebrew 47 formula; Linux build must still pass scripts/verify.py.
if ! .deps/ngspice-47/bin/ngspice --version 2>/dev/null | grep -q 'ngspice-47'; then
  mkdir -p .deps/ngspice-build
  curl --fail --location --retry 3 \
    https://downloads.sourceforge.net/project/ngspice/ng-spice-rework/47/ngspice-47.tar.gz \
    --output .deps/ngspice-build/ngspice-47.tar.gz
  python3 - <<'PY'
from pathlib import Path
import hashlib
p = Path('.deps/ngspice-build/ngspice-47.tar.gz')
if hashlib.sha256(p.read_bytes()).hexdigest() != '894e649651f1838a14095e5a5439e7d3aa63e87ede14d283173fda4fcdef675f':
    raise SystemExit('ngspice archive checksum mismatch')
PY
  tar -xzf .deps/ngspice-build/ngspice-47.tar.gz -C .deps/ngspice-build
  (
    cd .deps/ngspice-build/ngspice-47
    ./configure --prefix="$ANALOG_ROOT/.deps/ngspice-47" --without-x --enable-xspice --enable-cider --with-readline=yes --with-fftw3=yes
    make -j4
    make install
  )
fi
.venv-tpu/bin/python scripts/setup.py
.venv-tpu/bin/python -m training.preflight
.venv-tpu/bin/python -m training.preflight --tpu
.venv-tpu/bin/python scripts/verify.py
.venv-tpu/bin/python -m training.smoke
.venv-tpu/bin/python -m pip freeze > runs/environment/requirements-resolved.txt
printf '%s\n' "$ANALOG_REQUIREMENTS_SHA" > runs/environment/requirements-input.sha256
.venv-bootstrap/bin/uv --version > runs/environment/uv-version.txt
.venv-tpu/bin/python --version > runs/environment/python-version.txt
printf '%s\n' 'Bootstrap complete. Run source training/activate.sh in each shell.'
