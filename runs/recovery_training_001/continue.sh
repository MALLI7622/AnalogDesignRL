#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
source training/activate.sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
python -m training.worker --catalog datasets/analog_benchmark_250_v1/train_validation_catalog.json --mode research-pilot --pilot-manifest runs/recovery_training_001/continuation/manifest.json --output runs/recovery_training_001/worker_continuation --port 8766 --workers 4 > runs/recovery_training_001/worker_continuation.log 2>&1 &
pilot_worker_pid=$!
trap 'kill "$pilot_worker_pid" 2>/dev/null || true; wait "$pilot_worker_pid" 2>/dev/null || true' EXIT
sleep 2
timeout --kill-after=15s 900s python -m training.train --mode research-pilot --pilot-manifest runs/recovery_training_001/continuation/manifest.json --config runs/recovery_training_001/config.json --restore-checkpoint runs/research_pilot_007/train/checkpoints --restore-run runs/research_pilot_007/train/run.json --warm-start-adapter --output runs/recovery_training_001/train_continuation > runs/recovery_training_001/train_continuation.log 2>&1
timeout --kill-after=15s 300s python -m training.train --mode rollout --split validation --max-tasks 2 --episodes-per-task 1 --config runs/recovery_training_001/evaluation_config.json --restore-checkpoint runs/recovery_training_001/train_continuation/checkpoints --restore-run runs/recovery_training_001/train_continuation/run.json --output runs/recovery_training_001/after > runs/recovery_training_001/after.log 2>&1
