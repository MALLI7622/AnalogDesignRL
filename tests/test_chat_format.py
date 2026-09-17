"""CPU-only checks against the pinned tokenizer and installed Tunix collector."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest

from training.chat_format import GemmaChatParser, PromptWindowExceeded, verify_chat_contract, completion_alignment

SNAPSHOT = Path(__file__).resolve().parents[1] / '.cache/huggingface/hub/models--google--gemma-3-1b-it/snapshots/dcc83ea841ab6100d6b47a070329e1ba4cf78752'
AVAILABLE = SNAPSHOT.exists() and importlib.util.find_spec('tunix') and importlib.util.find_spec('transformers')


@unittest.skipUnless(AVAILABLE, 'requires locally cached pinned tokenizer and optional Tunix dependencies')
class ChatFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tunix.generate.tokenizer_adapter import Tokenizer
        cls.tokenizer = Tokenizer('huggingface', str(SNAPSHOT), add_bos=False, add_eos=False)
        cls.reference = cls.tokenizer.tokenizer

    def setUp(self):
        self.parser = GemmaChatParser(self.tokenizer)
        self.messages = [{'role': 'system', 'content': 'Return parameter JSON.'},
                         {'role': 'user', 'content': ' C=5; permitted range [1, 10]. '},
                         {'role': 'assistant', 'content': '{"C":6}'},
                         {'role': 'user', 'content': 'Gain failed; current C=6.'}]

    def test_full_history_matches_reference_and_does_not_mutate(self):
        before = copy.deepcopy(self.messages)
        for count in (2, 3, 4):
            messages = self.messages[:count]
            for generation in (False, True):
                actual = self.parser.parse(messages, add_generation_prompt=generation, is_first_msg=True)
                expected = self.reference.apply_chat_template(messages, tokenize=False, add_generation_prompt=generation)
                self.assertEqual(actual, expected)
                self.assertEqual(actual.count('<bos>'), 1)
                self.assertEqual(actual.count('<end_of_turn>'), count - 1)
        self.parser.preprocess_messages(self.messages)
        self.assertEqual(self.messages, before)

    def test_incremental_training_tokens_match_full_inference_prompt(self):
        from tunix.rl.agentic.utils import tokenize_and_generate_masks
        initial, masks = tokenize_and_generate_masks(self.messages[:2], self.tokenizer, self.parser,
            contains_first_msg=True, contains_generation_msg=True)
        self.assertTrue(all(value == 0 for value in masks))
        generated = self.tokenizer.encode('{"C":6}', add_special_tokens=False)
        feedback, masks = tokenize_and_generate_masks(self.messages[3:], self.tokenizer, self.parser,
            contains_generation_msg=True)
        expected = self.reference.apply_chat_template(self.messages, tokenize=True, add_generation_prompt=True)
        self.assertEqual(initial + generated + feedback, expected)
        self.assertTrue(all(value == 0 for value in masks))

    def test_assistant_mask_has_no_duplicate_role_header(self):
        from tunix.rl.agentic.utils import tokenize_and_generate_masks
        tokens, masks = tokenize_and_generate_masks([self.messages[2]], self.tokenizer, self.parser)
        self.assertEqual(self.tokenizer.decode(tokens), '{"C":6}<end_of_turn>\n')
        self.assertTrue(all(value == 1 for value in masks))

    def test_context_limit_is_checked_without_generation(self):
        parser = GemmaChatParser(self.tokenizer, max_prompt_tokens=2)
        with self.assertRaises(PromptWindowExceeded):
            parser.parse(self.messages, is_first_msg=True, add_generation_prompt=True)

    def test_startup_gate_rejects_old_formatter(self):
        from tunix.rl.agentic.parser.chat_template_parser.parser import GemmaChatTemplateParser
        self.assertEqual(verify_chat_contract(self.tokenizer)['status'], 'passed')
        with self.assertRaisesRegex(ValueError, 'Chat format'):
            verify_chat_contract(self.tokenizer, GemmaChatTemplateParser(self.tokenizer))

    def test_actual_completion_guard_detects_trimmed_or_unexpected_stop_tokens(self):
        from types import SimpleNamespace
        prompt = self.parser.parse(self.messages[:2], is_first_msg=True, add_generation_prompt=True)
        for response, suffix, valid in [('{"C":6}', '', True), (' {"C":6} ', '', False),
                                         ('{"C":6}', '<end_of_turn>', False)]:
            output = SimpleNamespace(text=[response], tokens=[self.tokenizer.encode(response + suffix, add_special_tokens=False)])
            self.assertEqual(completion_alignment(self.tokenizer, [prompt], output)['valid'], valid)

    def test_installed_collector_preserves_tokens_masks_and_prompt_history(self):
        import numpy as np
        from tunix.rl.agentic.agents.model_agent import ModelAgent
        from tunix.rl.agentic.environments.base_environment import BaseTaskEnv, EnvStepResult
        from tunix.rl.agentic.trajectory.trajectory_collect_engine import TrajectoryCollectEngine
        from tunix.rl.rollout.base_rollout import RolloutOutput

        class Env(BaseTaskEnv):
            def _initial_observation(self):
                return {'prompts': 'C=5; permitted range [1, 10].'}
            def _step_impl(self, action):
                return EnvStepResult(observation={'prompts': 'Gain failed; current C=6.'},
                                     reward=-.2, done=False, info={})
            def close(self):
                pass

        agent = ModelAgent(system_prompt='Return parameter JSON.')
        env = Env({'task_id': 'cpu-test'}, max_steps=2)
        sampled_prompts = []
        def fake_model(messages, env, **kwargs):
            before = copy.deepcopy(messages)
            prompt = self.parser.parse(messages, is_first_msg=True, add_generation_prompt=True)
            self.assertEqual(messages, before)
            self.assertEqual(prompt, self.reference.apply_chat_template(messages, tokenize=False, add_generation_prompt=True))
            sampled_prompts.append(self.tokenizer.encode(prompt, add_special_tokens=False))
            response = '{"C":6}' if len(sampled_prompts) == 1 else '{"C":7}'
            # Use the installed sampler's actual exclusive stop-token slicing.
            from tunix.generate.utils import np_find_first_eos_idx
            buffer = np.array(self.tokenizer.encode(response + '<end_of_turn>', add_special_tokens=False))
            tokens = buffer[:np_find_first_eos_idx(buffer, np.array([1, 106]))]
            return RolloutOutput(text=[response], logits=[], tokens=[tokens],
                left_padded_prompt_tokens=np.array([sampled_prompts[-1]]), logprobs=[np.zeros(len(tokens))])
        engine = TrajectoryCollectEngine(agent, env, model_call=fake_model,
            tokenizer=self.tokenizer, chat_parser=self.parser, max_response_length=1024)
        result = asyncio.run(engine.collect(mode='Token'))
        first, second = agent.trajectory.steps
        assembled_second = list(result['prompt_tokens']) + list(first.assistant_tokens) + list(first.env_tokens)
        self.assertEqual(assembled_second, sampled_prompts[1])
        self.assertTrue(np.all(first.env_masks == 0))
        self.assertTrue(np.all(first.assistant_masks == 1))
        self.assertTrue(np.all(second.assistant_masks == 1))
        self.assertEqual(len(result['conversation_tokens']), len(result['conversation_masks']))
        self.assertEqual(len(result['conversation_tokens']), len(result['old_logprobs']))
        self.assertEqual(int(sum(result['conversation_masks'])), len(first.assistant_tokens) + len(second.assistant_tokens))
        self.assertEqual(agent.chat_completions[1]['content'], 'C=5; permitted range [1, 10].')
