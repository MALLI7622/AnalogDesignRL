"""Verify group advantages and accumulated optimizer updates survive splitting."""
from collections import namedtuple
import importlib.util
import unittest
from unittest.mock import Mock
AVAILABLE = all(importlib.util.find_spec(name) for name in ('numpy', 'jax', 'optax', 'tunix'))
if AVAILABLE:
    import numpy as np
    import jax
    import jax.numpy as jnp
    import optax
    from tunix.rl.common import aggregate_loss
from training.train import install_sequence_accumulation, sequence_microbatches

Example = namedtuple('Example', 'completion_ids advantages completion_mask')


@unittest.skipUnless(AVAILABLE, 'optional TPU training dependencies are not installed')
class SequenceMicrobatchTests(unittest.TestCase):
    def example(self):
        return Example(jnp.arange(12, dtype=jnp.float32).reshape(4, 3) / 12,
                       jnp.array([-1., .2, .3, .5]),
                       jnp.array([[1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 0, 0]]))

    def test_accumulated_update_matches_full_group(self):
        example = self.example()
        chunks = list(sequence_microbatches([example], 4, 1))
        np.testing.assert_array_equal(jnp.concatenate([x.advantages for x in chunks]), example.advantages)
        def loss(weight, batch):
            per_token = (weight * batch.completion_ids - batch.advantages[:, None]) ** 2
            return aggregate_loss(per_token, batch.completion_mask, 'sequence-mean-token-mean')
        weight = jnp.array(.4)
        optimizer = optax.chain(optax.clip_by_global_norm(.1), optax.adamw(3e-3))
        update, _ = optimizer.update(jax.grad(loss)(weight, example), optimizer.init(weight), weight)
        expected = optax.apply_updates(weight, update)
        accumulated = optax.MultiSteps(optimizer, 4)
        state = accumulated.init(weight)
        for index, chunk in enumerate(chunks):
            update, state = accumulated.update(jax.grad(loss)(weight, chunk), state, weight)
            weight = optax.apply_updates(weight, update)
            if index < 3:
                self.assertAlmostEqual(float(weight), .4, places=6)
        np.testing.assert_allclose(weight, expected, rtol=1e-6)

    def test_wrapper_splits_train_and_eval_and_rejects_partial_group(self):
        cluster = Mock()
        original = cluster.update_actor
        install_sequence_accumulation(cluster, 4, 1)
        cluster.update_actor([self.example()], [self.example()], False)
        train, evaluation, skip = original.call_args.args
        self.assertEqual(len(train), 4)
        self.assertEqual(len(evaluation), 4)
        self.assertFalse(skip)
        with self.assertRaises(ValueError):
            list(sequence_microbatches([train[0]], 4, 1))

    def test_empty_response_mask_is_rejected_without_changing_loss_normalization(self):
        example = self.example()
        example = example._replace(completion_mask=example.completion_mask.at[0].set(0))
        with self.assertRaisesRegex(ValueError, 'empty response masks'):
            list(sequence_microbatches([example], 4, 1))
