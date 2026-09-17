"""Exercise the installed Tunix observation contract without model weights."""
import importlib.util
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(importlib.util.find_spec('tunix') and importlib.util.find_spec('numpy'),
                     'optional TPU training dependencies are not installed')
class TunixAdapterTests(unittest.TestCase):
    def test_microbatch_id_and_initial_observation_match_grpo_contract(self):
        import numpy as np
        from training.tunix_env import CircuitEnvironment
        from tunix.rl.agentic.agents.model_agent import ModelAgent
        client = Mock()
        client.request.return_value = {'episode_id': 'test-episode', 'mode': 'research-pilot',
            'specification': {'constraints': {}, 'current_parameters': {'C': 1e-12}, 'evaluations_remaining': 30}}
        with patch('training.tunix_env.WorkerClient', return_value=client):
            env = CircuitEnvironment({'task_id': np.array(['task-one'])}, endpoint='http://127.0.0.1:8765',
                                     max_steps=4, required_mode='research-pilot')
            observation, info = env.reset()
            client.request.assert_called_once_with('/episodes', {'task_id': 'task-one'})
            self.assertIsInstance(observation['prompts'], str)
            agent = ModelAgent(system_prompt='Return JSON')
            agent.update_from_env(observation, 0.0, False, info)
            self.assertIn('prompts', agent.trajectory.task)
            self.assertEqual(agent.chat_completions[-1]['content'], observation['prompts'])
            env.close()


if __name__ == '__main__':
    unittest.main()
