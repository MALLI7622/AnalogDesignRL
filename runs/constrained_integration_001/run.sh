#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
source training/activate.sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
python -m training.worker --catalog datasets/analog_benchmark_250_v1/train_validation_catalog.json --mode evaluation --output runs/constrained_integration_001/worker --port 8766 --workers 4 > runs/constrained_integration_001/worker.log 2>&1 &
audit_worker_pid=$!
trap 'kill "$audit_worker_pid" 2>/dev/null || true; wait "$audit_worker_pid" 2>/dev/null || true' EXIT
sleep 2
timeout --kill-after=15s 600s python -m training.train --mode rollout --split validation --max-tasks 1 --episodes-per-task 2 --config runs/constrained_integration_001/config.json --restore-checkpoint runs/research_pilot_007/train/checkpoints --restore-run runs/research_pilot_007/train/run.json --output runs/constrained_integration_001/rollout > runs/constrained_integration_001/trainer.log 2>&1
