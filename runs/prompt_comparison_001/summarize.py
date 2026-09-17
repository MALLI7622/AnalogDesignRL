"""Report the preregistered paired prompt comparison from raw rollout evidence."""
import json
from collections import Counter, defaultdict
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
PLAN = json.loads((ROOT / 'plan.json').read_text())
TASKS = [task['id'] for task in PLAN['tasks']]


def summarize(name):
    path = ROOT / name
    run = json.loads((path / 'run.json').read_text())
    paths = [path]
    continuation = ROOT / (name + '_continuation')
    if continuation.exists():
        other_run = json.loads((continuation / 'run.json').read_text())
        if (other_run['config'] != run['config']
                or other_run['restore']['adapter_sha256'] != run['restore']['adapter_sha256']
                or other_run['prompt'] != run['prompt']):
            raise RuntimeError('Continuation changed prompt, model, or configuration')
        paths.append(continuation)
    if run['selection']['rollout_ids'] != TASKS:
        raise RuntimeError('Unexpected task selection')
    rows = [json.loads(line) for part in paths
            for line in (part / 'rollouts.jsonl').read_text().splitlines()]
    errors = {}
    for part in paths:
        error_file = part / 'episode_errors.jsonl'
        if error_file.exists():
            for line in error_file.read_text().splitlines():
                error = json.loads(line)
                key = (error['task_id'], error['episode'])
                if key in errors or error['kind'] != 'context_overflow':
                    raise RuntimeError('Duplicate or unexpected episode error')
                errors[key] = error
    groups = defaultdict(list)
    for row in rows:
        groups[(row['task_id'], row['episode'])].append(row)
    expected = {(task, episode) for task in TASKS for episode in range(1, 5)}
    if set(groups) != expected:
        raise RuntimeError('Incomplete episode coverage')
    for key, episode in groups.items():
        if [r['step'] for r in episode] != list(range(1, len(episode) + 1)):
            raise RuntimeError('Nonconsecutive episode steps')
        if len(episode) > 4 or (len(episode) < 4 and not episode[-1]['success'] and key not in errors):
            raise RuntimeError('Incomplete or over-budget episode')
        if key in errors and errors[key]['completed_attempts'] != len(episode):
            raise RuntimeError('Error/action count mismatch')
    finals = {key: rows[-1] for key, rows in groups.items()}
    task_metrics = {task: {'episodes': 4, 'solved': 0, 'evaluations': 0,
                          'failed_actions': 0, 'valid_changed_actions': 0,
                          'episodes_with_valid_change': 0, 'valid_designs': set(),
                          'final_rewards': [], 'unchanged_design_rewards': [],
                          'valid_changed_rewards': [], 'episode_best_valid_rewards': []} for task in TASKS}
    statuses = Counter()
    invalid_json = fenced_replies = 0
    for (task, _), row in finals.items():
        task_metrics[task]['final_rewards'].append(row['verifier_reward'])
        task_metrics[task]['solved'] += bool(row['success'])
    trajectories = [p for part in paths for p in (part / 'trajectories').glob('*.jsonl')]
    if len(trajectories) != len(expected):
        raise RuntimeError('Trajectory count mismatch')
    feedback_count = 0
    for trajectory in trajectories:
        events = [json.loads(line) for line in trajectory.read_text().splitlines()]
        initial = events[0]
        task = initial['task_id']
        start = initial['specification']['current_parameters']
        metrics = task_metrics[task]
        changed_in_episode = False
        valid_rewards = []
        for event in events:
            if event['event'] == 'action':
                invalid_json += not isinstance(event.get('parsed_action'), dict)
                fenced_replies += event.get('response', '').lstrip().startswith('```')
            if event['event'] != 'feedback':
                continue
            obs = event['observation']
            feedback_count += 1
            metrics['evaluations'] += 1
            statuses[obs['status']] += 1
            if obs['status'] == 'ok':
                candidate = obs['parameters']
                changed = candidate != start
                metrics['valid_changed_actions'] += changed
                changed_in_episode |= changed
                metrics['valid_designs'].add(json.dumps(candidate, sort_keys=True))
                valid_rewards.append(obs['reward'])
                metrics['valid_changed_rewards' if changed else 'unchanged_design_rewards'].append(obs['reward'])
            else:
                metrics['failed_actions'] += 1
        metrics['episodes_with_valid_change'] += changed_in_episode
        metrics['episode_best_valid_rewards'].append(max(valid_rewards) if valid_rewards else None)
    if feedback_count != len(rows):
        raise RuntimeError('Feedback/rollout evidence mismatch')
    for metrics in task_metrics.values():
        starts = metrics.pop('unchanged_design_rewards')
        if starts and max(starts) - min(starts) > 1e-8:
            raise RuntimeError('Unchanged design produced inconsistent scores')
        metrics['observed_start_reward'] = starts[0] if starts else None
        metrics['unique_valid_designs'] = len(metrics.pop('valid_designs'))
        metrics['mean_final_reward'] = statistics.mean(metrics['final_rewards'])
        metrics['final_reward_std'] = statistics.pstdev(metrics['final_rewards'])
    seeds = {}
    for line in [line for part in paths for line in (part / 'generation_seeds.jsonl').read_text().splitlines()]:
        row = json.loads(line)
        key = (row['task_id'], row['episode'], row['episode_call'])
        if key in seeds:
            raise RuntimeError('Duplicate seed context')
        seeds[key] = row['seed']
    if len(seeds) != len(rows):
        raise RuntimeError('Generation/action count mismatch')
    summary = {
        'episodes': len(finals), 'evaluations': len(rows),
        'context_overflow_episodes': len(errors), 'episode_errors': list(errors.values()),
        'simulator_invocations': sum(row['simulator_invocations'] for row in rows),
        'invalid_json_actions': invalid_json, 'markdown_fenced_replies': fenced_replies,
        'solved_episodes': sum(r['success'] for r in finals.values()),
        'tasks_solved_at_least_once': sum(m['solved'] > 0 for m in task_metrics.values()),
        'mean_final_reward': statistics.mean(r['verifier_reward'] for r in finals.values()),
        'failed_actions': sum(m['failed_actions'] for m in task_metrics.values()),
        'valid_changed_actions': sum(m['valid_changed_actions'] for m in task_metrics.values()),
        'episodes_with_valid_change': sum(m['episodes_with_valid_change'] for m in task_metrics.values()),
        'tasks_with_valid_change': sum(m['episodes_with_valid_change'] > 0 for m in task_metrics.values()),
        'tasks_with_final_reward_variation': sum(m['final_reward_std'] > 1e-12 for m in task_metrics.values()),
        'action_statuses': dict(statuses), 'per_task': task_metrics,
    }
    return summary, run, seeds, finals


current, current_run, current_seeds, current_finals = summarize('current')
exploration, exploration_run, exploration_seeds, exploration_finals = summarize('exploration')
for report in (current, exploration):
    improvements = 0
    episodes_improved = 0
    for task in TASKS:
        reference = current['per_task'][task]['observed_start_reward']
        if reference is None:
            raise RuntimeError('Missing empirical start score for comparison')
        metrics = report['per_task'][task]
        metrics['valid_changed_actions_better_than_start'] = sum(
            reward > reference + 1e-10 for reward in metrics['valid_changed_rewards'])
        metrics['episodes_with_better_design_than_start'] = sum(
            reward is not None and reward > reference + 1e-10
            for reward in metrics['episode_best_valid_rewards'])
        improvements += metrics['valid_changed_actions_better_than_start']
        episodes_improved += metrics['episodes_with_better_design_than_start']
    report['valid_changed_actions_better_than_start'] = improvements
    report['episodes_with_better_design_than_start'] = episodes_improved
a, b = current_run['config'].copy(), exploration_run['config'].copy()
a.pop('prompt_variant'); b.pop('prompt_variant')
if a != b or current_run['restore']['adapter_sha256'] != exploration_run['restore']['adapter_sha256']:
    raise RuntimeError('Model or non-prompt configuration mismatch')
shared = set(current_seeds) & set(exploration_seeds)
if any(current_seeds[key] != exploration_seeds[key] for key in shared):
    raise RuntimeError('Paired seed mismatch')
comparisons = Counter()
for key, before in current_finals.items():
    delta = exploration_finals[key]['verifier_reward'] - before['verifier_reward']
    comparisons['better' if delta > 1e-10 else 'worse' if delta < -1e-10 else 'tied'] += 1
result = {'current': current, 'exploration': exploration,
          'paired_episode_reward_comparison': dict(comparisons),
          'matching_seed_contexts': len(shared),
          'checkpoint_sha256': current_run['restore']['adapter_sha256'],
          'limitations': PLAN['limitations']}
(ROOT / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
lines = ['# Larger exploration-prompt comparison', '',
         'Both prompts used the same two-update Gemma 3 1B checkpoint on all 21 validation tasks, '
         'four independent episodes per task, and at most four attempts per episode. '
         'Seeds were paired by task, episode, and attempt. No optimizer updates were run.', '',
         '| Metric | Current prompt | Exploration prompt |', '|---|---:|---:|']
for key, label in [('solved_episodes', 'Solved episodes / 84'),
                   ('tasks_solved_at_least_once', 'Tasks solved at least once / 21'),
                   ('mean_final_reward', 'Mean final reward (higher is better)'),
                   ('evaluations', 'Circuit evaluations'), ('failed_actions', 'Failed actions'),
                   ('context_overflow_episodes', 'Episodes stopped by context overflow'),
                   ('invalid_json_actions', 'Replies rejected as invalid JSON actions'),
                   ('simulator_invocations', 'Simulator invocations'),
                   ('valid_changed_actions', 'Valid actions changing starting parameters'),
                   ('episodes_with_valid_change', 'Episodes with a valid parameter change / 84'),
                   ('tasks_with_valid_change', 'Tasks with a valid parameter change / 21'),
                   ('valid_changed_actions_better_than_start', 'Valid changed actions scoring better than the starting design'),
                   ('episodes_with_better_design_than_start', 'Episodes finding a better design than the start / 84'),
                   ('tasks_with_final_reward_variation', 'Tasks with final-reward variation / 21')]:
    x, y = current[key], exploration[key]
    if isinstance(x, float): x, y = f'{x:.6f}', f'{y:.6f}'
    lines.append(f'| {label} | {x} | {y} |')
lines += ['', f'Paired episode rewards: {dict(comparisons)}.', '',
          '**Outcome:** the combined exploration prompt failed. Every reply used Markdown fences '
          'and was rejected before simulation. The current prompt retained valid formatting but '
          'never changed the starting design. Neither variant demonstrated useful exploration. '
          'Keep the current default; next isolate a change-one-parameter instruction while preserving '
          'the working JSON scaffold. This comparison cannot distinguish the effects of the two prompt changes.', '',
          'A valid changed action must pass the evaluator action/simulation checks and differ from '
          'the initial parameter vector. Repeated changed designs still count as changed actions; '
          'per-task unique design counts are in `results.json`. Reward variation can also come from failed actions.', '',
          'Starting-design reference scores come from successful simulations of unchanged designs in the current-prompt arm. '
          'Better-than-start means a strictly higher fixed verifier score, not necessarily a solved circuit.', '',
          PLAN['limitations'], '',
          'Context-overflow episodes remain in the denominator; their last observed verifier score is retained. '
          'The exploration process initially aborted on overflow after 62 full episodes and three attempts in episode 63. '
          'A continuation skipped those 63 attempted pairs and ran the remaining 21 with the same settings and paired seeds. '
          'Subsequent overflows were logged per episode. No episodes were replayed and no context limits were enlarged.', '',
          'The exploration variant changes the system instructions and removes the repeated current-value '
          'answer example from each observation. This tests the combined prompt change, not either component alone.', '',
          'Evidence: `plan.json`, prompt configs, `source_snapshot/`, both `run.json` files, '
          '`rollouts.jsonl`, `generation_seeds.jsonl`, and full `trajectories/`.', '',
          'Files are local to the TPU VM, scheduled to terminate at 2026-09-17 06:53 UTC. No external backup was created.', '']
(ROOT / 'README.md').write_text('\n'.join(lines))
print(json.dumps({name: {k:v for k,v in report.items() if k != 'per_task'}
                  for name, report in [('current', current), ('exploration', exploration)]}, indent=2))
