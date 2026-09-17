"""Summarize matched validation rollouts without exposing private solutions."""
import json
from collections import Counter
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent


def summarize(name):
    run = ROOT / name
    record = json.loads((run / 'run.json').read_text())
    steps = [json.loads(line) for line in (run / 'rollouts.jsonl').read_text().splitlines()]
    groups = {}
    for step in steps:
        groups.setdefault((step['task_id'], step['episode']), []).append(step)
    if len(groups) != 8 or any(len(rows) != 4 and not rows[-1]['success'] for rows in groups.values()):
        raise RuntimeError('Evaluation is incomplete')
    finals = [rows[-1] for rows in groups.values()]
    changed = 0
    statuses = Counter()
    for path in (run / 'trajectories').glob('*.jsonl'):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        start = events[0]['specification']['current_parameters']
        for event in events:
            if event['event'] == 'feedback':
                obs = event['observation']
                statuses[obs['status']] += 1
                changed += bool(obs.get('parameters') and obs['parameters'] != start)
    return {
        'episodes': len(finals), 'evaluations': len(steps),
        'solved': sum(row['success'] for row in finals),
        'mean_final_reward': statistics.mean(row['verifier_reward'] for row in finals),
        'changed_parameter_actions': changed, 'action_statuses': dict(statuses),
        'task_ids': record['selection']['rollout_ids'],
        'per_task': {task: {'solved': sum(r['success'] for r in finals if r['task_id'] == task),
                           'mean_final_reward': statistics.mean(r['verifier_reward'] for r in finals if r['task_id'] == task)}
                     for task in record['selection']['rollout_ids']},
        'restore': record['restore'],
    }


baseline = summarize('baseline')
trained = summarize('trained')
if baseline['task_ids'] != trained['task_ids']:
    raise RuntimeError('Task selection mismatch')
records = [json.loads((ROOT / name / 'run.json').read_text()) for name in ('baseline', 'trained')]
if records[0]['config'] != records[1]['config']:
    raise RuntimeError('Evaluation configuration mismatch')
result = {'baseline': baseline, 'trained': trained,
          'training': json.loads((ROOT / 'train/training_result.json').read_text()),
          'limitations': 'Two optimizer updates; two validation tasks with four sampled episodes each. No held-out test-set or unseen-topology claim. Local artifacts on an expiring TPU VM.'}
(ROOT / 'performance.json').write_text(json.dumps(result, indent=2) + '\n')
lines = ['# Gemma analog-design pilot: training and matched evaluation', '',
         'Two optimizer updates completed. Adapter weights changed, and checkpoint 2 passed readback verification.', '',
         '| Metric | Original model | Trained model |', '|---|---:|---:|',
         f"| Solved episodes | {baseline['solved']}/8 | {trained['solved']}/8 |",
         f"| Mean final reward | {baseline['mean_final_reward']:.6f} | {trained['mean_final_reward']:.6f} |",
         f"| Failed actions | {baseline['action_statuses'].get('failed', 0)}/32 | {trained['action_statuses'].get('failed', 0)}/32 |",
         f"| Circuit evaluations | {baseline['evaluations']} | {trained['evaluations']} |",
         f"| Actions changing parameters | {baseline['changed_parameter_actions']} | {trained['changed_parameter_actions']} |",
         '', 'Higher reward is better; success earns +1. Failed constraints receive negative scores.', '',
         'The trained model produced fewer failed actions in this sample, but neither model changed the starting parameters or solved a circuit. Useful circuit optimization is not demonstrated.', '',
         result['limitations'], '',
         'The existing TPU is scheduled to terminate at 2026-09-17 06:53 UTC. Preserve this run externally before then. No external backup was created.', '',
         'The matched runs use identical task order, four-attempt episode limits, temperature, base seed, and per-call seed policy. '
         'Baseline evaluation reloads the original model; trained evaluation reloads the verified adapter. '
         'The separate initial validation inside training uses a different rollout schedule.', '',
         'The training pilot used 16 episodes / 64 evaluations; the matched comparison used another 16 episodes / 64 evaluations. '
         'Previous attempts remain separate and used 16 episodes / 60 evaluations.', '',
         'Evidence: `performance.json`, `scalar_metrics.json`, `train/training_result.json`, '
         '`baseline/rollouts.jsonl`, `trained/rollouts.jsonl`, and per-episode `trajectories/`.', '']
(ROOT / 'README.md').write_text('\n'.join(lines))
print(json.dumps({'baseline': baseline, 'trained': trained}, indent=2))
