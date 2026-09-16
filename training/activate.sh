#!/usr/bin/env bash
# Source from the repository root in each TPU VM shell: source training/activate.sh
if ! test -f training/configs/gemma3_1b.json; then
  printf '%s\n' 'Run source training/activate.sh from the repository root.' >&2
  return 1
fi
ANALOG_ROOT="$(pwd -P)"
umask 077
export UV_PYTHON_INSTALL_DIR="$ANALOG_ROOT/.deps/python"
export UV_CACHE_DIR="$ANALOG_ROOT/.cache/uv"
export HF_HOME="$ANALOG_ROOT/.cache/huggingface"
export JAX_COMPILATION_CACHE_DIR="$ANALOG_ROOT/.cache/jax"
export JAX_COMPILATION_CACHE_MAX_SIZE=21474836480
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export PATH="$ANALOG_ROOT/.deps/ngspice-47/bin:$PATH"
mkdir -p "$HF_HOME" "$JAX_COMPILATION_CACHE_DIR"
if test -f .venv-tpu/bin/activate; then
  source .venv-tpu/bin/activate
fi
if test -f .cache/worker.token; then
  export ANALOG_WORKER_TOKEN="$(cat .cache/worker.token)"
fi
