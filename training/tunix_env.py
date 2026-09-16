"""Thin Tunix 0.1.7 adapter; only imported in the TPU Python environment."""
import os

from tunix.rl.agentic.environments.base_environment import BaseTaskEnv, EnvStepResult
from training.client import RemoteEpisode, WorkerClient


class CircuitEnvironment(BaseTaskEnv):
    def __init__(self, task, *, endpoint, max_steps, required_mode="training", **kwargs):
        if required_mode not in {"training", "research-pilot"}:
            raise ValueError("Optimizer environments require training or explicitly authorized research-pilot mode")
        super().__init__(task, max_steps=max_steps, **kwargs)
        client = WorkerClient(endpoint, os.environ.get("ANALOG_WORKER_TOKEN"))
        self.remote = RemoteEpisode(client, task["task_id"], max_steps=max_steps, required_mode=required_mode)

    def _initial_observation(self):
        return {"question": self.remote.reset()}

    def _step_impl(self, action):
        observation, reward, done, info = self.remote.step(action)
        return EnvStepResult(observation={"question": observation}, reward=reward, done=done, info=info)

    def close(self):
        self.remote.close()
