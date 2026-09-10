#!/usr/bin/env bash
# Run manually on the allocated Ubuntu TPU VM, from the repository root.
set -euo pipefail
test -f training/requirements-tpu.txt
ANALOG_ROOT="$(pwd -P)"
export UV_PYTHON_INSTALL_DIR="$ANALOG_ROOT/.deps/python"
export UV_CACHE_DIR="$ANALOG_ROOT/.cache/uv"

sudo apt-get update
sudo apt-get install -y python3-venv python3-pip build-essential bison flex libreadline-dev libfftw3-dev curl git pkg-config
python3 -m venv .venv-bootstrap
.venv-bootstrap/bin/python -m pip install uv
.venv-bootstrap/bin/uv python install 3.12
.venv-bootstrap/bin/uv venv --python 3.12 .venv-tpu
.venv-bootstrap/bin/uv pip install --python .venv-tpu/bin/python pip
.venv-tpu/bin/python -m pip install -r training/requirements-tpu.txt
.venv-tpu/bin/python -m pip check

# Same ngspice source release as the current verifier. Checksum from the locally
# installed Homebrew 47 formula; Linux build must still pass scripts/verify.py.
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
export PATH="$ANALOG_ROOT/.deps/ngspice-47/bin:$PATH"
.venv-tpu/bin/python scripts/setup.py
.venv-tpu/bin/python -m training.preflight
.venv-tpu/bin/python -m training.preflight --tpu
mkdir -p runs/environment
.venv-tpu/bin/python -m pip freeze > runs/environment/requirements-resolved.txt
.venv-bootstrap/bin/uv --version > runs/environment/uv-version.txt
.venv-tpu/bin/python scripts/verify.py
.venv-tpu/bin/python -m training.smoke
printf '%s\n' 'Bootstrap complete. Activate .venv-tpu and add .deps/ngspice-47/bin to PATH in each shell.'
