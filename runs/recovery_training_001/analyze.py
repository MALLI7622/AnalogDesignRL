"""Export readable rollout/optimizer evidence; never infer success from a process launch."""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def rows(path):
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []

def write(path, obj):
    path.write_text(json.dumps(obj, indent=2, allow_nan=False)+'\n')

report={'stages':{}, 'verified_optimizer_updates':None}
for stage in ('before','train','train_continuation','after'):
    directory=ROOT/stage
    if not directory.exists():
        continue
    generations=rows(directory/'generations.jsonl')
    responses=[r for r in generations if r['event']=='response']
    requests=[r for r in generations if r['event']=='request']
    episodes=[]
    histories=sorted([rows(p) for p in (directory/'trajectories').glob('*.jsonl')],key=lambda h:h[0]['timestamp_utc'])
    for index,history in enumerate(histories,1):
        initial=history[0]
        episode={'episode_index':index,'task_id':initial['task_id'],'episode_id':initial['episode_id'],
                 'initial_specification':initial['specification'],'steps':[]}
        current=dict(initial['specification']['current_parameters'])
        for event in history:
            if event['event']=='action':
                step={'step':event['step'],'raw_response':event['response'],'parsed_action':event['parsed_action']}
            elif event['event']=='feedback':
                obs=event['observation']
                changed={k:{'before':current.get(k),'after':v} for k,v in obs['parameters'].items() if current.get(k)!=v}
                assert sorted(changed)==obs['parameters_changed']
                step.update(applied_changes=changed,feedback=obs)
                episode['steps'].append(step)
                current=dict(obs['parameters'])
        episodes.append(episode)
        write(directory/f"{episode['task_id']}_ep{index}.json",episode)
    steps=[s for ep in episodes for s in ep['steps']]
    final_scores=[ep['steps'][-1]['feedback']['reward'] for ep in episodes if ep['steps']]
    summary={'model_requests':sum(r['prompt_count'] for r in requests),
             'model_responses':sum(len(r['responses']) for r in responses),
             'alignment_passes':sum(sum(r.get('training_token_alignment',{}).get('per_completion',[])) for r in responses),
             'episodes_started':len(episodes),'evaluated_actions':len(steps),
             'valid_actions':sum(s['feedback']['action_valid'] for s in steps),
             'applied_parameter_changes':sum(bool(s['applied_changes']) for s in steps),
             'successful_measurements':sum(s['feedback']['measurement_success'] for s in steps),
             'solved_episodes':sum(any(s['feedback']['success'] for s in ep['steps']) for ep in episodes),
             'simulator_invocations':sum(s['feedback']['simulator_invocations'] for s in steps),
             'final_scores':final_scores,'mean_final_score':sum(final_scores)/len(final_scores) if final_scores else None}
    report['stages'][stage]=summary
    write(directory/'trajectories.json',episodes)
report['reward_groups']=rows(ROOT/'train_continuation/reward_groups.jsonl')
result_path=ROOT/'train_continuation/training_result.json'
if result_path.exists():
    result=json.loads(result_path.read_text())
    report['verified_optimizer_updates']=result['optimizer_steps']
    report['training_result']=result
elif report['reward_groups'] and not report['reward_groups'][0]['has_learning_signal'] and report['reward_groups'][0]['optimizer_steps_before_group']==0:
    report['verified_optimizer_updates']=0
    report['stop_reason']='First training group has zero reward variation; guard stopped before likelihood/optimizer work.'
if 'after' in report['stages']:
    pairs={stage:[(r['task_id'],r['episode'],r['episode_call'],r['seed']) for r in rows(ROOT/stage/'generation_seeds.jsonl')] for stage in ('before','after')}
    common=set(pairs['before'])&set(pairs['after'])
    report['paired_evaluation_seed_count']=len(common)
    before_ids=[ep['task_id'] for ep in json.loads((ROOT/'before/trajectories.json').read_text())]
    after_ids=[ep['task_id'] for ep in json.loads((ROOT/'after/trajectories.json').read_text())]
    assert before_ids==after_ids
diagnostics=Counter(json.loads(p.read_text()).get('error') for p in (ROOT/'worker_continuation').glob('*/evaluation_*/result.json'))
report.update(training_failure_diagnostics=dict(diagnostics),
    total_model_responses=sum(s['model_responses'] for s in report['stages'].values()),
    total_simulator_invocations=sum(s['simulator_invocations'] for s in report['stages'].values()))
if report['verified_optimizer_updates']==0:
    report['after_evaluation']='Not run: no optimizer update or trained checkpoint.'
    report['checkpoint_zero']='Tunix wrote an untrained step-0 snapshot; it is not a completed training checkpoint and fails positive-step restore validation.'
write(ROOT/'results.json',report)
print(json.dumps(report,indent=2))
