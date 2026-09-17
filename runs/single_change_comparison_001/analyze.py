"""Build readable, paired results from recorded evaluator feedback."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')

configs = {arm: json.loads((ROOT / arm / 'config.json').read_text()) for arm in ('baseline', 'explicit')}
assert {k:v for k,v in configs['baseline'].items() if k != 'prompt_variant'} == {k:v for k,v in configs['explicit'].items() if k != 'prompt_variant'}
results = {'optimizer_updates': 0, 'arms': {}}
seeds = {}
for arm in configs:
    rollout = ROOT / arm / 'rollout'
    rows = read_jsonl(rollout / 'rollouts.jsonl')
    generations = read_jsonl(rollout / 'generations.jsonl')
    responses = [r for r in generations if r['event'] == 'response']
    seeds[arm] = [(r['task_id'], r['episode'], r['episode_call'], r['seed']) for r in responses]
    histories = sorted([read_jsonl(p) for p in (rollout / 'trajectories').glob('*.jsonl')], key=lambda h:h[0]['timestamp_utc'])
    episodes = []
    for index, history in enumerate(histories, 1):
        spec = history[0]['specification']
        start = spec['current_parameters']
        current = dict(start)
        steps = []
        for event in history:
            if event['event'] == 'action':
                action = event['parsed_action']
                proposed = {k: {'before':current[k], 'proposed':v} for k,v in action.items() if k in current and current[k] != v}
                violations = []
                for key, value in action.items():
                    bound = spec['parameters'].get(key)
                    if bound is None:
                        violations.append(f'Unknown parameter: {key}')
                    elif not bound['min'] <= value <= bound['max']:
                        violations.append(f"{key}={value} outside [{bound['min']}, {bound['max']}]")
                    elif bound.get('integer') and int(value) != value:
                        violations.append(f'{key} must be an integer')
                step = {'step':event['step'], 'raw_response':event['response'], 'action':action, 'proposed_changes':proposed, 'bound_violations':violations}
            elif event['event'] == 'feedback':
                obs = event['observation']
                # A valid action may run and change state even when measurement extraction fails.
                accepted = not violations and obs['simulator_invocations'] > 0
                changes = {k:{'before':current[k], 'after':v} for k,v in obs['parameters'].items() if current[k] != v}
                step.update(accepted_for_simulation=accepted, measurement_success=obs['status']=='ok', accepted_parameter_change=accepted and bool(changes), applied_changes=changes, follows_single_change_instruction=accepted and len(action)==1 and len(changes)==1, feedback=obs)
                current = dict(obs['parameters'])
                steps.append(step)
        unsubmitted = [{'step':r['episode_call']+1, 'raw_response':r['responses'][0]} for r in responses if r['episode']==index and r['episode_call']+1 not in {s['step'] for s in steps}]
        episode = {'episode':index, 'task_id':spec['id'], 'initial_specification':spec, 'steps':steps, 'responses_not_submitted_to_evaluator':unsubmitted}
        episodes.append(episode)
        save(ROOT / arm / f"{spec['id']}_ep{index}.json", episode)
    all_steps = [s for e in episodes for s in e['steps']]
    summary = {'model_calls':len(responses), 'evaluated_actions':len(all_steps), 'alignment_passes':sum(r['training_token_alignment']['valid'] for r in responses), 'actions_accepted_for_simulation':sum(s['accepted_for_simulation'] for s in all_steps), 'successful_measurements':sum(s['measurement_success'] for s in all_steps), 'accepted_parameter_changes':sum(s['accepted_parameter_change'] for s in all_steps), 'single_change_compliant_actions':sum(s['follows_single_change_instruction'] for s in all_steps), 'solved_episodes':sum(any(s['feedback']['success'] for s in e['steps']) for e in episodes), 'simulator_invocations':sum(r['simulator_invocations'] for r in rows), 'final_scores':[e['steps'][-1]['feedback']['reward'] if e['steps'] else None for e in episodes], 'responses_not_evaluated':len(responses)-len(all_steps)}
    assert len(rows)==len(all_steps)<=len(responses)<=4
    results['arms'][arm] = summary
    save(ROOT / arm / 'trajectories.json', episodes)
assert seeds['baseline'] == seeds['explicit'][:len(seeds['baseline'])]
results['paired_seeds_verified'] = True
results['paired_seeds'] = seeds
results['comparison_limitation'] = 'Baseline aborted on episode 2 step 1 duplicate keys before evaluator submission; no retry. Compare common seeds and report unequal actual counts.'
save(ROOT / 'results.json', results)
print(json.dumps(results, indent=2))
