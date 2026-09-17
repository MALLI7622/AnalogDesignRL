"""Replay saved prompt construction on CPU; no model weights or generation."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path


def main():
    os.environ['JAX_PLATFORMS'] = 'cpu'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--episodes', type=Path, required=True)
    cli.add_argument('--snapshot', type=Path, required=True)
    cli.add_argument('--output', type=Path, required=True)
    args = cli.parse_args()
    from tunix.generate.tokenizer_adapter import Tokenizer
    from tunix.rl.agentic.parser.chat_template_parser.parser import GemmaChatTemplateParser
    from training.chat_format import GemmaChatParser, verify_chat_contract
    from training.client import action_prompt, observation_text
    tokenizer = Tokenizer('huggingface', str(args.snapshot.resolve()), add_bos=False, add_eos=False)
    parser = GemmaChatParser(tokenizer)
    gate = verify_chat_contract(tokenizer)
    legacy = GemmaChatTemplateParser(tokenizer)
    episodes = json.loads(args.episodes.read_text())
    checks = []
    for episode in episodes:
        variant = 'current' if episode['arm'] == 'current' else 'exploration_v1'
        messages = [{'role': 'system', 'content': episode['system_prompt']},
                    {'role': 'user', 'content': action_prompt(observation_text(episode['specification']), variant)}]
        for step in episode['steps']:
            before = copy.deepcopy(messages)
            original = legacy.parse(copy.deepcopy(messages), is_first_msg=True, add_generation_prompt=True)
            canonical = tokenizer.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            corrected = parser.parse(messages, is_first_msg=True, add_generation_prompt=True)
            if original != step['prompt']:
                raise RuntimeError(f"Saved prompt reconstruction mismatch: {episode['task_id']} / {step['step']}")
            if corrected != canonical or messages != before:
                raise RuntimeError('Corrected parser differs from reference or mutates history')
            checks.append({'arm': episode['arm'], 'task_id': episode['task_id'], 'episode': episode['episode'],
                           'step': step['step'], 'saved_prompt_reproduced': True, 'canonical_match': True,
                           'canonical_tokens': len(tokenizer.encode(corrected, add_special_tokens=False)),
                           'added_end_of_turn_markers': corrected.count('<end_of_turn>') - original.count('<end_of_turn>'),
                           'added_bos': corrected.startswith('<bos>') and not original.startswith('<bos>')})
            messages.extend([{'role': 'assistant', 'content': step['response']},
                             {'role': 'user', 'content': action_prompt(observation_text(step['feedback']), variant)}])
    report = {'status': 'passed', 'scope': 'Offline CPU tokenization only; no inference, weight loading, or learning',
              'episodes': len(episodes), 'recorded_prompts_reproduced': len(checks),
              'corrected_prompts_matching_reference': len(checks),
              'max_corrected_prompt_tokens': max(row['canonical_tokens'] for row in checks),
              'prompts_exceeding_6144_tokens': sum(row['canonical_tokens'] > 6144 for row in checks),
              'source_episode_file_sha256': hashlib.sha256(args.episodes.read_bytes()).hexdigest(),
              'chat_template_sha256': hashlib.sha256(tokenizer.tokenizer.chat_template.encode()).hexdigest(),
              'startup_contract': gate, 'checks': checks}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key:value for key,value in report.items() if key != 'checks'}, indent=2))


if __name__ == '__main__':
    main()
