"""CPU grammar and actual Tunix token-selection checks; no model weights."""
import unittest
import importlib.util
import json
from pathlib import Path
from training.constrained_json import KeyGrammar, make_sampler

SNAPSHOT=Path(__file__).resolve().parents[1]/'.cache/huggingface/hub/models--google--gemma-3-1b-it/snapshots/dcc83ea841ab6100d6b47a070329e1ba4cf78752'

class GrammarTests(unittest.TestCase):
    def setUp(self): self.g=KeyGrammar(['CAPACITOR_0','CAPACITOR_1','CURRENT_0_BIAS'])
    def test_accepts_numeric_objects(self):
        for s in ['{}',' { "CAPACITOR_0" : -1.25e-10, "CURRENT_0_BIAS": 0 }\n','{"CAPACITOR_1":2E+3}']:
            self.assertEqual(self.g.validate(s),json.loads(s))
    def test_rejects_typos_injections_and_truncation(self):
        for s in ['{"CAPACATOR_0":1}','{"CAPACITOR_00":1}','{"CAPACITOR_0":true}',
                  '{"CAPACITOR_0":1,}','{"CAPACITOR_0":01}','{"CAPACITOR_0":1e}',
                  '{"CAPACITOR_0":1','```json\n{}\n```','{}junk','{"CAPACITOR_0":1,"CAPACITOR_0":2}']:
            with self.subTest(text=s),self.assertRaises(ValueError): self.g.validate(s)

    def test_schema_is_task_specific_and_bounds_are_separate(self):
        with self.assertRaises(ValueError): KeyGrammar(['OTHER']).validate('{"CAPACITOR_0":1}')
        self.assertEqual(self.g.validate('{"CAPACITOR_1":2.52e-10}'), {'CAPACITOR_1':2.52e-10})


@unittest.skipUnless(SNAPSHOT.exists() and importlib.util.find_spec('tunix') and importlib.util.find_spec('transformers'),
                     'requires local pinned tokenizer and optional Tunix dependencies')
class SamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tunix.generate.tokenizer_adapter import Tokenizer
        cls.t=Tokenizer('huggingface',str(SNAPSHOT),add_bos=False,add_eos=False)
        cls.g=KeyGrammar(['CAPACITOR_0','CAPACITOR_1'])
        cls.ids,cls.table,cls.lookup=cls.g.compile(cls.t)
    def test_real_token_paths_and_typo_are_distinguished(self):
        for text,valid in [('{"CAPACITOR_0":6.183e-11}',True),('{"CAPACATOR_0":1}',False)]:
            state=0
            for token in self.t.encode(text,add_special_tokens=False):
                if state<0: break
                state=int(self.table[state,self.lookup[token]])
            self.assertEqual(state==13,valid)
        # EOS cannot be selected before the closing brace.
        self.assertTrue(all(self.table[s,-2]<0 for s in range(len(self.table)) if s!=13))
    def test_jitted_sampler_blocks_highest_logit_typo(self):
        import jax
        import jax.numpy as jnp
        from flax import nnx
        from types import SimpleNamespace
        from tunix.generate.sampler import CacheConfig,_SamplingState
        class Dummy(nnx.Module):
            def __call__(self): pass
        original=SimpleNamespace(transformer=Dummy(),tokenizer=self.t,
            cache_config=CacheConfig(cache_size=64,num_layers=1,num_kv_heads=1,head_dim=1))
        sampler=make_sampler(original,self.g,self.t)
        prefix=self.t.encode('{"CAPAC',add_special_tokens=False)
        wrong=self.t.encode('A',add_special_tokens=False)[0]
        right=self.t.encode('I',add_special_tokens=False)[0]
        buffer=jnp.zeros((1,64),dtype=jnp.int32).at[0,0].set(2).at[0,1:1+len(prefix)].set(jnp.array(prefix))
        state=_SamplingState(decoding_step=len(prefix),token_buffer=buffer,positions=jnp.zeros_like(buffer),
            cache={},done=jnp.array([False]),total_sampling_steps=64,logits_buffer=None,
            logprobs_buffer=jnp.zeros((1,64)),forbidden_token_ids=None,seed=jax.random.key(1),
            sampling_mode='greedy',num_input_tokens=1,temperature=1.0,sampling_parameters={})
        logits=jnp.full((1,1,len(self.lookup)),-100.).at[0,0,wrong].set(100.).at[0,0,right].set(50.)
        result=jax.jit(sampler._sample)(logits,jnp.array([1,106]),{},state)
        self.assertEqual(int(result.token_buffer[0,len(prefix)+1]),right)
        self.assertTrue(bool(jnp.isfinite(result.logprobs_buffer[0,len(prefix)+1])))
        # The stochastic production mode must enforce the same mask.
        state=state.replace(sampling_mode='top_p', sampling_parameters={'top_p':1.0,'top_k':50})
        result=jax.jit(sampler._sample)(logits,jnp.array([1,106]),{},state)
        self.assertNotEqual(int(result.token_buffer[0,len(prefix)+1]),wrong)

if __name__=='__main__': unittest.main()
