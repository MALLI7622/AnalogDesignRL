#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
source training/activate.sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
python -c 'import jax; assert jax.default_backend() == "tpu"; print(jax.devices())' > runs/single_change_comparison_001/devices.log 2>&1
python -m training.worker --catalog datasets/analog_benchmark_250_v1/train_validation_catalog.json --mode evaluation --output runs/single_change_comparison_001/worker --port 8766 --workers 4 > runs/single_change_comparison_001/worker.log 2>&1 &
audit_worker_pid=$!
trap 'kill "$audit_worker_pid" 2>/dev/null || true; wait "$audit_worker_pid" 2>/dev/null || true' EXIT
sleep 2
for arm in baseline explicit; do
    timeout --kill-after=15s 300s python -m training.train --mode rollout --split validation --max-tasks 1 --episodes-per-task 2 --config "runs/single_change_comparison_001/$arm/config.json" --restore-checkpoint runs/research_pilot_007/train/checkpoints --restore-run runs/research_pilot_007/train/run.json --output "runs/single_change_comparison_001/$arm/rollout" > "runs/single_change_comparison_001/$arm/trainer.log" 2>&1
done
