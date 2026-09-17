import json
from pathlib import Path
root=Path(__file__).resolve().parent
base=root.parent/'formatter_smoke_001'
def read(p):
 return [json.loads(x) for x in p.read_text().splitlines()]
def analyze(p):
 rows=read(p/'rollout/rollouts.jsonl'); gens=[x for x in read(p/'rollout/generations.jsonl') if x['event']=='response']
 initial={}
 for f in (p/'rollout/trajectories').glob('*.jsonl'):
  for d in read(f):
   if d['event']=='initial': initial=d['specification']['current_parameters']; break
 changes=[]
 for r in rows:
  obs=json.loads(r['observation']); changes.append(obs['status']=='ok' and obs['parameters']!=initial)
 return {'calls':len(gens),'accepted':sum(json.loads(r['observation'])['status']=='ok' for r in rows),'accepted_changed_actions':sum(changes),'aligned':sum(x['training_token_alignment']['valid'] for x in gens),'solved_episodes':sum(r['success'] for r in rows),'final_scores':[r['verifier_reward'] for r in rows if r['step']==2 or r['success']],'responses':[r['response'] for r in rows],'seeds':[(x['task_id'],x['episode'],x['episode_call'],x['seed']) for x in gens],'simulator_invocations':sum(r['simulator_invocations'] for r in rows)}
a,b=analyze(base),analyze(root)
assert a['seeds']==b['seeds']
ca=json.loads((base/'rollout/run.json').read_text())['config']; cb=json.loads((root/'rollout/run.json').read_text())['config']
assert {k:v for k,v in ca.items() if k!='prompt_variant'}=={k:v for k,v in cb.items() if k!='prompt_variant'}
result={'baseline':a,'no_answer_example':b,'paired_seeds_match':True,'other_config_matches':True,'optimizer_updates':0}
(root/'results.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
