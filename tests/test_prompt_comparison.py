"""Prompt isolation and paired randomness for the exploration comparison."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest

from training.client import action_prompt, system_prompt, SYSTEM_PROMPT
from training.train import SeededGeneration, rollout_requests, argument_parser, validate_run_arguments


@dataclass
class Config:
    seed: int = 42


class PromptComparisonTests(unittest.TestCase):
    def test_continuation_preserves_episode_numbers(self):
        tasks = [{'id': 'a'}, {'id': 'b'}]
        full = list(rollout_requests(tasks, 4))
        self.assertEqual(list(rollout_requests(tasks, 4, 3)), full[3:])
        self.assertEqual(list(rollout_requests(tasks, 4, 7)), [(tasks[1], 4)])
        for flags in (['--mode', 'train', '--skip-rollout-episodes', '1'],
                      ['--mode', 'rollout', '--skip-rollout-episodes', '-1']):
            with self.assertRaises(ValueError):
                validate_run_arguments(argument_parser().parse_args(flags))

    def test_exploration_preserves_observation_without_repeated_answer_example(self):
        observation = json.dumps({'current_parameters': {'C': 5e-12},
                                  'parameters': {'C': {'min': 1e-12, 'max': 1e-11}},
                                  'constraints': {'gain_db': {'min': 60}}})
        current = action_prompt(observation)
        exploration = action_prompt(observation, 'exploration_v1')
        self.assertIn(observation, current)
        self.assertIn(observation, exploration)
        self.assertIn('required reply format are:', current)
        self.assertNotIn('required reply format are:', exploration)
        self.assertIn('change at least one allowed parameter', exploration)
        self.assertEqual(system_prompt(), SYSTEM_PROMPT)
        with self.assertRaises(ValueError):
            action_prompt(observation, 'unknown')

    def test_pairing_survives_early_termination_and_changes_between_episodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            def new(name):
                return SeededGeneration(lambda prompts, cfg: cfg.seed, 42, Path(tmp) / name)
            a, b = new('a.jsonl'), new('b.jsonl')
            for sampler in (a, b):
                sampler.set_episode('task-a', 1)
            self.assertEqual(a(['old prompt'], Config()), b(['new prompt'], Config()))
            a(['old prompt, second attempt'], Config())
            for sampler in (a, b):
                sampler.set_episode('task-b', 1)
            paired = a(['old prompt'], Config())
            self.assertEqual(paired, b(['new prompt'], Config()))
            b.set_episode('task-b', 2)
            self.assertNotEqual(paired, b(['new prompt'], Config()))
            rows = [json.loads(line) for line in (Path(tmp) / 'b.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['episode'], 2)
            self.assertEqual(rows[-1]['episode_call'], 0)
