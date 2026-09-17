"""Build a local, self-contained review of the exact recorded trajectories."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'review'
(OUT / 'episodes').mkdir(parents=True, exist_ok=True)
episodes = []


def pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False)


for part in ('current', 'exploration', 'exploration_continuation'):
    run = json.loads((ROOT / part / 'run.json').read_text())
    requests = {}
    for line in (ROOT / part / 'generations.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row['event'] == 'request':
            assert hashlib.sha256(row['prompts'][0].encode()).hexdigest() == row['prompt_sha256'][0]
            requests[(row['task_id'], row['episode'], row['episode_call'] + 1)] = row
    errors = {}
    error_path = ROOT / part / 'episode_errors.jsonl'
    if error_path.exists():
        for line in error_path.read_text().splitlines():
            row = json.loads(line)
            errors[(row['task_id'], row['episode'])] = row
    traces = []
    for path in (ROOT / part / 'trajectories').glob('*.jsonl'):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        traces.append((events[0]['timestamp_utc'], path, events))
    traces.sort()
    pairs = [(task, episode) for task in run['selection']['rollout_ids'] for episode in range(1, 5)]
    pairs = pairs[run['selection'].get('skip_rollout_episodes', 0):]
    for (_, path, events), (task, ordinal) in zip(traces, pairs, strict=False):
        initial = events[0]
        assert initial['task_id'] == task
        actions = {row['step']: row for row in events if row['event'] == 'action'}
        feedback = {row['step']: row['observation'] for row in events if row['event'] == 'feedback'}
        steps = []
        for step, action in sorted(actions.items()):
            request = requests[(task, ordinal, step)]
            obs = feedback[step]
            start = initial['specification']['current_parameters']
            note = ('Accepted; parameters are unchanged from the start.' if obs['status'] == 'ok' and obs['parameters'] == start
                    else 'Accepted; parameters differ from the start.' if obs['status'] == 'ok'
                    else 'Rejected before simulation: not a valid raw JSON object.' if not isinstance(action['parsed_action'], dict)
                    else 'Action or simulation failed; inspect feedback and allowed parameter names/bounds.')
            steps.append({'step': step, 'seed': request['seed'], 'prompt': request['prompts'][0],
                          'response': action['response'], 'parsed_action': action['parsed_action'],
                          'feedback': obs, 'note': note})
        arm = 'current' if part == 'current' else 'exploration'
        error = errors.get((task, ordinal))
        item = {'arm': arm, 'part': part, 'task_id': task, 'episode': ordinal,
                'episode_id': initial['episode_id'], 'specification': initial['specification'],
                'steps': steps, 'error': error, 'raw_trace': str(path.relative_to(ROOT)),
                'system_prompt': run['prompt']['system']}
        item['file'] = f'{arm}_{task}_ep{ordinal}.md'
        episodes.append(item)
        lines = [f'# {arm}: {task}, episode {ordinal}', '',
                 '[Back to index](../README.md)', '',
                 f"[Raw event trace](../../{item['raw_trace']}) · [Generation transcript](../../{part}/generations.jsonl)", '',
                 'These are recorded model replies and evaluator results. Notes are review annotations, not model reasoning.', '',
                 '## Starting task', '', '```json', pretty(item['specification']), '```', '',
                 '## System prompt', '', '````text', item['system_prompt'], '````', '']
        for step in steps:
            lines += [f"## Attempt {step['step']} — seed {step['seed']}", '', step['note'], '',
                      '**Exact model reply:**', '', '````text', step['response'], '````', '',
                      '**Evaluator feedback:**', '', '```json', pretty(step['feedback']), '```', '',
                      '<details><summary>Exact serialized prompt sent for this attempt</summary>', '',
                      '````text', step['prompt'], '````', '', '</details>', '']
        if error:
            lines += ['## Episode stopped: context overflow', '', '```json', pretty(error), '```', '']
        (OUT / 'episodes' / item['file']).write_text('\n'.join(lines))

assert len(episodes) == 168
assert sum(len(item['steps']) for item in episodes) == 671
episodes.sort(key=lambda item: (item['task_id'], item['episode'], item['arm']))
copy_example = next(e for e in episodes if e['arm'] == 'current' and all(s['feedback']['status'] == 'ok' for s in e['steps']))
reject_example = next(e for e in episodes if e['arm'] == 'exploration')
context_example = next(e for e in episodes if e['error'])
failed_current = next(e for e in episodes if e['arm'] == 'current' and any(s['feedback']['status'] != 'ok' for s in e['steps']))
featured = [('Copying the same values despite failed constraints', copy_example),
            ('Exploration prompt: fenced JSON rejected repeatedly', reject_example),
            ('Current prompt: a failed action among accepted copies', failed_current),
            ('Verbose output eventually exceeds the context window', context_example)]
index = ['# Trajectory review', '', '[Open the searchable browser](viewer.html) · [Experiment results](../README.md)', '',
         'All 168 attempted episodes and 671 actions are included. Each page shows the starting task, '
         'exact model replies, evaluator feedback, and expandable exact prompts. No hidden chain-of-thought is recorded.', '',
         '## Start here', '']
for label, item in featured:
    index.append(f"- [{label}](episodes/{item['file']})")
index += ['', 'Look for unchanged parameter values after feedback, disallowed keys, Markdown fences, '
          'and whether an attempt actually reached simulation. A negative reward with status `ok` means '
          'the action was accepted but the circuit failed requirements. Status `failed` is an action or simulation failure.', '',
          '## All episodes', '', '| Task | Episode | Current prompt | Exploration prompt |', '|---|---:|---|---|']
lookup = {(e['task_id'], e['episode'], e['arm']): e for e in episodes}
for task in sorted({e['task_id'] for e in episodes}):
    for ordinal in range(1, 5):
        a, b = lookup[(task, ordinal, 'current')], lookup[(task, ordinal, 'exploration')]
        index.append(f"| {task} | {ordinal} | [Read](episodes/{a['file']}) | [Read](episodes/{b['file']}) |")
(OUT / 'README.md').write_text('\n'.join(index) + '\n')
(OUT / 'featured.json').write_text(pretty({label: item['file'] for label, item in featured}) + '\n')

payload = json.dumps(episodes, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
html = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analog design trajectory review</title><style>
body{font:16px system-ui;margin:0;color:#202b39;background:#f5f7fa}header{padding:20px 28px;background:#182b40;color:white}h1{margin:0;font-size:24px}
main{display:grid;grid-template-columns:330px 1fr;gap:20px;padding:20px}aside{position:sticky;top:20px;align-self:start}input,select,button{font:inherit;padding:9px;box-sizing:border-box}input,select{width:100%;margin-bottom:10px}#episodes{height:65vh;font-size:13px}
article{min-width:0}section,details{background:white;border:1px solid #d4dce5;border-radius:8px;padding:16px;margin-bottom:16px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 monospace;background:#f1f4f8;padding:14px;border-radius:5px}.note{color:#714014;font-weight:600}small{color:#556477}a{color:#12639d}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:8px;border-bottom:1px solid #ddd}@media(max-width:800px){main{display:block}aside{position:static}#episodes{height:180px}}
</style><header><h1>Analog design trajectory review</h1><p>168 episodes · exact replies, prompts and evaluator feedback · no external services</p></header>
<main><aside><label for="query">Find a task</label><input id="query" placeholder="Task ID or episode number"><label for="arm">Prompt</label><select id="arm"><option value="">Both prompts</option><option>current</option><option>exploration</option></select><label for="episodes">Episode</label><select id="episodes" size="20"></select><button id="pair">Show matching episode with the other prompt</button><p><small>Notes are review annotations. Recorded replies contain no accessible hidden chain-of-thought.</small></p></aside><article id="detail"></article></main>
<script id="data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('data').textContent),list=document.getElementById('episodes'),detail=document.getElementById('detail');let chosen=0;
function node(tag,text,parent){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.append(n);return n}
function block(title,value,parent,open=false){const d=node('details',undefined,parent);d.open=open;node('summary',title,d);node('pre',typeof value==='string'?value:JSON.stringify(value,null,2),d)}
function show(i){chosen=i;const e=data[i];detail.replaceChildren();node('h2',e.arm+' · episode '+e.episode,detail);node('p',e.task_id,detail);const link=node('a','Markdown version',detail);link.href='episodes/'+e.file;node('p','Compare allowed parameter names and bounds with the reply, then inspect status, failed checks and simulator invocations.',detail);block('Starting task and allowed values',e.specification,detail);block('System prompt',e.system_prompt,detail);
for(const s of e.steps){const section=node('section',undefined,detail);node('h3','Attempt '+s.step+' · seed '+s.seed,section);node('p',s.note,section).className='note';node('h4','Exact model reply',section);node('pre',s.response,section);const f=s.feedback;node('p','Status: '+f.status+' | reward: '+f.reward+' | solved: '+f.success+' | simulator invocations: '+f.simulator_invocations,section);if(f.error)node('p',f.error,section);block('Full evaluator feedback',f,section);block('Exact serialized prompt sent to the model',s.prompt,section)}if(e.error)block('Episode stopped: context overflow',e.error,detail,true)}
function filter(){const q=document.getElementById('query').value.toLowerCase(),arm=document.getElementById('arm').value;list.replaceChildren();data.forEach((e,i)=>{if((!arm||arm===e.arm)&&(e.task_id+' '+e.episode).toLowerCase().includes(q)){const o=node('option',e.arm+' · '+e.task_id.replace('ramos_pfc_frontier_','')+' · ep '+e.episode+(e.error?' · CONTEXT ERROR':''),list);o.value=i}});if(list.options.length){list.selectedIndex=0;show(Number(list.value))}}
list.onchange=()=>show(Number(list.value));document.getElementById('query').oninput=filter;document.getElementById('arm').onchange=filter;document.getElementById('pair').onclick=()=>{const e=data[chosen],i=data.findIndex(x=>x.task_id===e.task_id&&x.episode===e.episode&&x.arm!==e.arm);document.getElementById('arm').value='';filter();list.value=i;show(i)};filter();
</script></html>'''
(OUT / 'viewer.html').write_text(html.replace('PAYLOAD', payload))
print(pretty({'episodes': len(episodes), 'attempts': sum(len(e['steps']) for e in episodes),
              'featured': {label: e['file'] for label, e in featured}}))
