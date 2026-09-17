"""Sampling seed isolation without importing JAX or allocating a TPU."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from training.train import SeededGeneration, argument_parser, rollout_requests, validate_run_arguments


@dataclass
class Config:
    seed: int = 42
    temperature: float = 0.9


class GenerationSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_identical_prompts_get_distinct_reproducible_seeds_without_config_mutation(self):
        config = Config()
        seen = []

        def generate(prompts, cfg, **kwargs):
            self.assertEqual(prompts, ["same task"])
            self.assertEqual(kwargs, {"test_option": True})
            self.assertEqual(cfg.temperature, config.temperature)
            self.assertIsNot(cfg, config)
            seen.append(cfg.seed)
            return cfg.seed

        first = SeededGeneration(generate, 42, self.root / "first.jsonl")
        results = [first(["same task"], config, test_option=True) for _ in range(8)]
        self.assertEqual(len(set(results)), 8)
        self.assertEqual(config.seed, 42)
        second = SeededGeneration(generate, 42, self.root / "second.jsonl")
        self.assertEqual(results, [second(["same task"], config, test_option=True) for _ in range(8)])
        self.assertEqual((self.root / "first.jsonl").read_text(), (self.root / "second.jsonl").read_text())
        records = [json.loads(line) for line in (self.root / "first.jsonl").read_text().splitlines()]
        self.assertEqual([r["seed"] for r in records], results)
        self.assertEqual(len({r["prompt_sha256"][0] for r in records}), 1)
        self.assertNotIn("same task", (self.root / "first.jsonl").read_text())

    def test_concurrent_calls_do_not_reuse_seeds(self):
        call = SeededGeneration(lambda prompts, cfg: cfg.seed, 2**32 - 1, self.root / "parallel.jsonl")
        with ThreadPoolExecutor(max_workers=4) as pool:
            seeds = list(pool.map(lambda _: call(["task"], Config()), range(100)))
        self.assertEqual(len(set(seeds)), 100)
        self.assertTrue(all(0 <= seed < 2**32 for seed in seeds))
        records = [json.loads(line) for line in (self.root / "parallel.jsonl").read_text().splitlines()]
        self.assertEqual([r["call"] for r in records], list(range(100)))

    def test_failure_consumes_seed_and_existing_ledger_is_preserved(self):
        seeds = []

        def generate(prompts, config):
            seeds.append(config.seed)
            if len(seeds) == 1:
                raise RuntimeError("sampler failed")
            return config.seed

        path = self.root / "failed.jsonl"
        call = SeededGeneration(generate, 42, path)
        with self.assertRaisesRegex(RuntimeError, "sampler failed"):
            call(["task"], Config())
        call(["task"], Config())
        self.assertNotEqual(*seeds)
        with self.assertRaises(FileExistsError):
            SeededGeneration(generate, 42, path)
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_invalid_seed_and_exhaustion_rejected(self):
        for seed in (-1, 2**32, True, 1.5, None):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                SeededGeneration(None, seed, self.root / "invalid.jsonl")
        call = SeededGeneration(None, 0, self.root / "exhausted.jsonl")
        call.calls = 2**32
        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            call(["task"], Config())

    def test_generation_transcript_preserves_prompt_and_raw_model_text(self):
        path = self.root / "generations.jsonl"
        raw = '```json\n{"x":1}\n```'
        call = SeededGeneration(lambda prompts, cfg: SimpleNamespace(text=[raw]), 42,
                                self.root / "seeds.jsonl", path)
        call(["full prompt"], Config())
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(rows[0]['prompts'], ['full prompt'])
        self.assertEqual(rows[1]['responses'], [raw])
        self.assertEqual(rows[0]['seed'], rows[1]['seed'])

    def test_single_string_prompt_logs_one_prompt_not_characters(self):
        seen = []
        def generate(prompts, cfg):
            seen.append(prompts)
            return SimpleNamespace(text=['{}'])
        path = self.root / 'single.jsonl'
        call = SeededGeneration(generate, 42, self.root / 'single_seeds.jsonl', path)
        call('one entire prompt', Config())
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(rows[0]['prompt_count'], 1)
        self.assertEqual(len(rows[0]['prompt_sha256']), 1)
        self.assertEqual(rows[0]['prompts'], ['one entire prompt'])
        self.assertEqual(seen, ['one entire prompt'])

    def test_repeated_episodes_are_explicit_and_do_not_change_task_selection(self):
        args = argument_parser().parse_args(["--mode", "rollout", "--episodes-per-task", "4"])
        validate_run_arguments(args)
        tasks = [{"id": "a"}, {"id": "b"}]
        requests = list(rollout_requests(tasks, 4))
        self.assertEqual([(t["id"], n) for t, n in requests],
                         [(t, n) for t in ("a", "b") for n in range(1, 5)])
        for flags in (["--mode", "train", "--episodes-per-task", "4"],
                      ["--mode", "rollout", "--episodes-per-task", "0"]):
            with self.assertRaises(ValueError):
                validate_run_arguments(argument_parser().parse_args(flags))


if __name__ == "__main__":
    unittest.main()
