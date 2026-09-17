import json
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parent
progress = {'checked_utc': datetime.now(timezone.utc).isoformat(), 'arms': {}}
for name in ('current', 'exploration'):
    p = root / name / 'rollouts.jsonl'
    if not p.exists():
        progress['arms'][name] = {'status': 'not_started'}
        continue
    paths = [p.parent]
    continuation = root / (name + '_continuation')
    if (continuation / 'rollouts.jsonl').exists():
        paths.append(continuation)
    rows = [json.loads(line) for part in paths
            for line in (part / 'rollouts.jsonl').read_text().splitlines()]
    completed = sum(row['step'] == 4 or row['success'] for row in rows)
    errors = sum(len((part / 'episode_errors.jsonl').read_text().splitlines())
                 for part in paths if (part / 'episode_errors.jsonl').exists())
    changes = failed = 0
    for trajectory in [p for part in paths for p in (part / 'trajectories').glob('*.jsonl')]:
        events = [json.loads(line) for line in trajectory.read_text().splitlines()]
        if not events:
            continue
        start = events[0]['specification']['current_parameters']
        for event in events:
            if event['event'] == 'feedback':
                obs = event['observation']
                failed += obs['status'] != 'ok'
                changes += obs['status'] == 'ok' and obs.get('parameters') != start
    progress['arms'][name] = {
        'status': 'episodes_complete' if completed + errors == 84 else 'in_progress',
        'completed_episodes': completed, 'evaluations': len(rows),
        'context_failures': errors, 'finished_episodes': completed + errors,
        'solved': sum(row['success'] for row in rows),
        'valid_changed_actions': changes, 'failed_actions': failed,
    }
(root / 'progress.json').write_text(json.dumps(progress, indent=2) + '\n')
print(json.dumps(progress, indent=2))
