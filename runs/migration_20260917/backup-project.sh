#!/usr/bin/env bash
set -euo pipefail
cd /home/cheriearjun/AnalogDesignRL
gcloud storage rsync . gs://molecule-lens/analog-design --recursive \
  --exclude='(^|.*/)(\.git|\.cache|\.deps|\.venv[^/]*|__pycache__|external)(/|$)|(^|.*/)\.env($|\.(?!example$))|.*\.pyc$|(^|.*/)\.DS_Store$|^training/cloud\.local\.json$'
