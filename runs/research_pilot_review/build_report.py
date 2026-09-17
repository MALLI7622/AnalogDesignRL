"""Build a human-readable view of saved pilot evidence; never runs a model."""
import csv
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
RUNS = [ROOT / 'runs' / f'research_pilot_{i:03d}' for i in (4, 5, 6)]


def link(path, parent=OUT):
    return os.path.relpath(path, parent)


def main():
    (OUT / 'episodes').mkdir(exist_ok=True)
    summary = []
    attempts = []
    for run in RUNS:
        auth = run / 'authorization.json'
        if not auth.exists():
            continue
        usage = ROOT / 'runs/research_pilot_usage' / (hashlib.sha256(auth.read_bytes()).hexdigest() + '.json')
        counters = json.loads(usage.read_text()) if usage.exists() else {}
        result = run / 'train/training_result.json'
        outcome = run / 'attempt_summary.json'
        attempts.append({'run': run.name, 'episodes_reserved': counters.get('episodes_reserved', 0),
                         'evaluations_reserved': counters.get('evaluations_reserved', 0),
                         'outcome': json.loads(outcome.read_text()) if outcome.exists() else None,
                         'training_result': json.loads(result.read_text()) if result.exists() else None})
        for trace in sorted((run / 'train/trajectories').glob('*.jsonl')):
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            initial = next(e for e in events if e['event'] == 'initial')
            feedback = [e for e in events if e['event'] == 'feedback']
            actions = [e for e in events if e['event'] == 'action']
            params = initial['specification']['current_parameters']
            changed_actions = sum(isinstance(a['parsed_action'], dict) and
                                  any(k in params and params[k] != v for k, v in a['parsed_action'].items())
                                  for a in actions)
            episode_id = initial['episode_id']
            name = f'{run.name}_{episode_id}.md'
            row = {'run': run.name, 'task': initial['task_id'], 'episode_id': episode_id,
                   'evaluations': len(feedback), 'simulator_invocations': sum(f['observation']['simulator_invocations'] for f in feedback),
                   'final_reward': feedback[-1]['observation']['reward'] if feedback else None,
                   'success': any(f['observation']['success'] for f in feedback),
                   'actions_changing_start_values': changed_actions, 'report': 'episodes/' + name}
            summary.append(row)
            doc = [f"# {initial['task_id']}", '', f"Run: `{run.name}`. Episode: `{episode_id}`.", '',
                   f"[Raw event trace]({link(trace, OUT / 'episodes')})", '',
                   '## Initial public specification', '', '```json',
                   json.dumps(initial['specification'], indent=2), '```', '']
            for action in actions:
                step = action['step']
                doc += [f'## Attempt {step}', '', 'Model reply:', '', '````text', str(action['response']), '````', '']
                result_event = next((f for f in feedback if f['step'] == step), None)
                if result_event:
                    obs = result_event['observation']
                    doc += [f"Reward: **{obs['reward']}**. Success: **{obs['success']}**. "
                            f"Simulator invocations: **{obs['simulator_invocations']}**.", '',
                            'Full feedback:', '', '```json', json.dumps(obs, indent=2), '```', '']
                else:
                    doc += ['No feedback event was saved for this action.', '']
            closure = next((e for e in events if e['event'] == 'closed'), None)
            doc += ['## Closure', '', '```json', json.dumps(closure, indent=2), '```', '']
            (OUT / 'episodes' / name).write_text('\n'.join(doc))
    report = {'attempts': attempts, 'episodes': summary,
              'total_episodes_reserved': sum(a['episodes_reserved'] for a in attempts),
              'total_evaluations_reserved': sum(a['evaluations_reserved'] for a in attempts)}
    (OUT / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    doc = ['# Research pilot trajectories', '',
           '**Outcome: no optimizer update completed.** The final attempt ran out of TPU memory at the first gradient update '
           '(34.64 GiB needed versus 15.75 GiB available per device). No trained checkpoint or improvement is established.', '',
           'Experimental training diagnostic; independent expert review remains pending. '
           'These are local VM files, not an external backup.', '',
           f"Total reserved: **{report['total_episodes_reserved']} episodes / {report['total_evaluations_reserved']} evaluations**. "
           'Original cumulative cap: 20 episodes / 80 evaluations.', '',
           '## Attempts and logs', '']
    for a, run in zip(attempts, RUNS):
        doc += [f"- **{a['run']}**: {a['episodes_reserved']} episodes, {a['evaluations_reserved']} evaluations. "
                f"[Trainer log]({link(run / 'trainer.log')}); "
                f"[Authorization]({link(run / 'authorization.json')}); "
                f"[Outcome]({link(run / 'attempt_summary.json')})."]
    doc += ['', '## Episode index', '',
            '| Run | Task | Evaluations | Final reward | Solved | Actions changing start | Trajectory |',
            '|---|---|---:|---:|---|---:|---|']
    for row in summary:
        doc.append(f"| {row['run']} | {row['task']} | {row['evaluations']} | {row['final_reward']} | "
                   f"{row['success']} | {row['actions_changing_start_values']} | [Read]({row['report']}) |")
    doc += ['', '## Additional evidence', '',
            '- Each run retains `train/generations.jsonl`: exact prompts and raw model replies.',
            '- `train/trajectories/` contains per-episode machine-readable events.',
            '- `worker/<episode_id>/` contains the evaluator trajectory and raw simulations.',
            '- `train/metrics/trajectory_log_*.csv` records Tunix trajectory status and token masks.',
            '- Attempt 005 logged string prompt lengths as `prompt_count`; raw prompt strings are intact. '
            'This logging defect was fixed before attempt 006.',
            '- Attempt 006 retains `source_snapshot/` and `config.json` for the executed code and configuration.',
            '', '## What to improve next', '',
            '1. Reduce gradient memory by splitting sequences into smaller microbatches and accumulating gradients '
            'while preserving the four-sample GRPO comparison.',
            '2. Profile token padding. Observed maxima were 3,433 prompt tokens and 2,387 trajectory tokens, '
            'but other tasks may need more.',
            '3. Address copying: every saved action retained the starting values or misspelled a parameter. '
            'A prompt example containing all current values may encourage copying; this is a hypothesis to test. '
            'Consider a small training-only supervised warm-up if prompt changes do not produce valid parameter exploration.',
            '4. After a successful bounded update, verify finite losses, changed weights, checkpoint readback, '
            'and matched before/after performance before increasing the training budget.',
            '', 'A checkpoint file alone does not prove learning. Inspect the final outcome, weight-change checks, '
            'reward variation, and checkpoint readback before drawing conclusions.', '']
    (OUT / 'README.md').write_text('\n'.join(doc))


if __name__ == '__main__':
    main()
