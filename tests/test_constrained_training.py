"""CPU tests of grammar probabilities and the installed Tunix GRPO loss."""
import json
import unittest
import importlib.util
from types import SimpleNamespace
import numpy as np
from training.constrained_training import Constraints, masked_distribution, training_probabilities, prompt_names, require_probability_agreement, reward_group_summary

class TinyTokenizer:
    """Compositional ASCII test tokenizer; Gemma boundary IDs preserved."""
    special={'<pad>':0,'<bos>':2,'<start_of_turn>':105,'<end_of_turn>':106}
    all_special_ids=list(special.values())
    def __init__(self): self.tokenizer=self
    def get_vocab(self): return {**{chr(i):1000+i for i in range(128)},**self.special}
    def encode(self,s,**kwargs):
        out=[]
        while s:
            match=next((k for k in self.special if s.startswith(k)),None)
            if match: out.append(self.special[match]); s=s[len(match):]
            else: out.append(1000+ord(s[0])); s=s[1:]
        return out
    def decode(self,ids,**kwargs):
        inverse={v:k for k,v in self.special.items()}
        return ''.join(inverse[i] if i in inverse else chr(i-1000) for i in ids)
    def convert_tokens_to_ids(self,t): return self.get_vocab()[t]


def prompt(t,names):
    return t.encode('Circuit task and feedback:\n'+json.dumps({'parameters':{n:{} for n in names},
        'current_parameters':{n:1 for n in names}},separators=(',',':'))+'<end_of_turn>\n<start_of_turn>model\n')

@unittest.skipUnless(importlib.util.find_spec('tunix'), 'requires optional Tunix')
class TrainingConstraintsTests(unittest.TestCase):
    def setUp(self):
        self.t=TinyTokenizer(); self.c=Constraints(self.t)
    def test_optimizer_gate_rejects_observed_tpu_gap(self):
        with self.assertRaisesRegex(ValueError,'refusing optimizer'):
            require_probability_agreement([[0.,-1.]],[[0.,-.743]],[[1,1]])
        require_probability_agreement([[0.,-1.]],[[0.,-.999]],[[1,1]])
        with self.assertRaises(ValueError): require_probability_agreement([[0.]],[[float('nan')]],[[1]])

    def test_generation_accepts_actual_agentic_single_string_and_rollout_list(self):
        from dataclasses import dataclass
        from unittest.mock import Mock, patch
        from training.constrained_training import ConstrainedGeneration
        @dataclass
        class Config:
            top_k: int = 50
            top_p: float = .9
        rendered=self.t.decode(prompt(self.t,['C']))
        rollout=SimpleNamespace(generate=Mock(return_value='result'), _sampler=object())
        generator=ConstrainedGeneration(rollout,self.c)
        with patch('training.constrained_training.make_sampler') as factory:
            for value in (rendered,[rendered]):
                self.assertEqual(generator(value,Config()),'result')
                passed, config=rollout.generate.call_args.args
                self.assertEqual(passed,value)
                self.assertEqual((config.top_k,config.top_p),(0,1.0))
            factory.assert_called_once()

    def test_zero_reward_variation_stops_before_parent_likelihood_work(self):
        from unittest.mock import Mock, patch
        from tunix.rl.agentic.agentic_grpo_learner import GRPOLearner
        cluster=SimpleNamespace(get_ref_per_token_logps=Mock(), get_actor_per_token_logps=Mock(), get_old_per_token_logps=Mock())
        with training_probabilities(cluster,self.c,.9,require_variation=True) as Learner:
            learner=object.__new__(Learner)
            learner.algo_config=SimpleNamespace(num_generations=4)
            with patch.object(GRPOLearner,'_process_results') as parent:
                with self.assertRaisesRegex(RuntimeError,'Zero reward variation'):
                    learner._process_results([SimpleNamespace(traj={'trajectory_reward':-1.}) for _ in range(4)])
                parent.assert_not_called()
        self.assertTrue(reward_group_summary([-1.,-.1,-1.,-.1],4)['has_learning_signal'])
        for rewards in ([-1.], [float('nan')]*4):
            with self.assertRaises(ValueError): reward_group_summary(rewards,4)

    def test_training_requires_validated_precision_before_worker_access(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from training.train import main
        config=json.loads((Path(__file__).resolve().parents[1]/'training/configs/gemma3_1b.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.json'; p.write_text(json.dumps(config))
            with patch('sys.argv',['train','--mode','train','--config',str(p)]),patch('training.train.WorkerClient') as client:
                with self.assertRaisesRegex(ValueError,'explicit float32'): main()
                client.assert_not_called()
    def test_multiturn_feedback_padding_and_schema_isolation(self):
        p=prompt(self.t,['C'])
        first=self.t.encode('{"C":1}')
        feedback=self.t.encode('<end_of_turn>\n<start_of_turn>user\n{"CAPACATOR_0":9}<end_of_turn>\n<start_of_turn>model\n')
        last=self.t.encode('{"C":2}')
        completion=first+feedback+last+[0,0]
        mask=[1]*len(first)+[0]*len(feedback)+[1]*len(last)+[0,0]
        meta=self.c.metadata([p],[completion],[mask])
        np.testing.assert_array_equal(meta['active'][0],mask)
        with self.assertRaisesRegex(ValueError,'disagree'): self.c.metadata([p],[completion],[np.ones(len(completion))])
        with self.assertRaisesRegex(ValueError,'violates'): self.c.metadata([p],[self.t.encode('{"D":1}')])
        with self.assertRaisesRegex(ValueError,'Incomplete'): self.c.metadata([p],[self.t.encode('{"C":')])

    def test_probabilities_normalized_temperature_and_forbidden_gradient(self):
        import jax
        import jax.numpy as jnp
        p=prompt(self.t,['C']); completion=self.t.encode('{"C":1}')
        meta=self.c.metadata([p],[completion]); vocab=1128
        logits=jnp.arange(vocab,dtype=jnp.float32)[None,None,:]*0.01
        logits=jnp.broadcast_to(logits,(1,len(completion),vocab))/.9
        logps,masked=masked_distribution(logits,meta)
        i=completion.index(1000+ord('1'))
        allowed=meta['token_ids'][0,meta['allowed'][0,i]]
        ref=np.asarray(logits)[0,i,allowed]; ref=ref-np.max(ref)
        expected=float(np.asarray(logits)[0,i,completion[i]]-np.max(np.asarray(logits)[0,i,allowed])-np.log(np.exp(ref).sum()))
        self.assertAlmostEqual(float(logps[0,i]),expected,places=5)
        probs=jax.nn.softmax(masked,axis=-1)
        np.testing.assert_allclose(np.asarray(probs.sum(-1)),1,atol=1e-6)
        self.assertTrue(np.all(np.asarray(probs)[~meta['allowed']]==0))
        grad=jax.grad(lambda x:masked_distribution(x,meta)[0].sum())(logits)
        self.assertTrue(np.isfinite(np.asarray(grad)).all())
        self.assertEqual(float(grad[0,i,1000+ord('A')]),0.)

    def test_actual_grpo_actor_anchor_reference_and_gradient(self):
        import jax
        import jax.numpy as jnp
        from flax import nnx
        from tunix.rl import common
        from tunix.rl.agentic.agentic_grpo_learner import GRPOConfig
        class TinyModel(nnx.Module):
            def __init__(self): self.bias=nnx.Param(jnp.arange(1128,dtype=jnp.float32)*.013)
            def __call__(self,tokens,positions=None,cache=None,attention_mask=None,**kwargs):
                return jnp.broadcast_to(self.bias[...].astype(jnp.bfloat16),(*tokens.shape,1128)), None
        actor=TinyModel(); reference=TinyModel()
        def forward(model,prompt_tokens,completion_tokens,**kwargs):
            graph,state=nnx.split(model)
            # Match RLCluster's actual host-side inference microbatch loop.
            return jnp.concatenate([common.compute_per_token_logps(graph,state,
                prompt_tokens=prompt_tokens[i:i+1],completion_tokens=completion_tokens[i:i+1],
                pad_id=0,eos_id=106,temperature=.9) for i in range(len(prompt_tokens))])
        cluster=SimpleNamespace(get_ref_per_token_logps=lambda **kw:forward(reference,**kw),
            get_actor_per_token_logps=lambda **kw:forward(actor,**kw),
            get_old_per_token_logps=lambda **kw:forward(actor,**kw))
        p=jnp.asarray([prompt(self.t,['C'])]*2)
        replies=['{"C":1}','{"C":2}']; c=jnp.asarray([self.t.encode(s) for s in replies])
        original=common.compute_per_token_logps
        with training_probabilities(cluster,self.c,.9) as Learner:
            old=cluster.get_actor_per_token_logps(prompt_tokens=p,completion_tokens=c)
            ref=cluster.get_ref_per_token_logps(prompt_tokens=p,completion_tokens=c)
            np.testing.assert_allclose(old,ref,atol=1e-6)
            raw=jnp.broadcast_to(actor.bias[...].astype(jnp.bfloat16).astype(jnp.float32),(*c.shape,1128))/.9
            expected,_=masked_distribution(raw,self.c.metadata(p,c))
            np.testing.assert_allclose(old,expected,atol=1e-6)
            example=Learner.example_type(prompt_ids=p,prompt_mask=p!=0,completion_ids=c,
                completion_mask=jnp.ones_like(c),advantages=jnp.array([1.,-1.]),
                ref_per_token_logps=ref,old_per_token_logps=old,
                constraint_metadata=jax.tree.map(jnp.asarray,self.c.metadata(p,c)))
            config=GRPOConfig(num_generations=2,beta=.08)
            config.temperature=.9
            def loss(model): return Learner.constraint_loss(model,example,config,0,106)
            (value,aux),grad=nnx.value_and_grad(loss,has_aux=True)(actor)
            self.assertTrue(np.isfinite(float(value)))
            self.assertAlmostEqual(float(aux['is_ratio/mean']),1.,places=5)
            self.assertAlmostEqual(float(aux['kl']),0.,places=5)
            self.assertTrue(np.isfinite(float(aux['entropy'])))
            leaves=jax.tree.leaves(grad)
            self.assertTrue(all(np.isfinite(np.asarray(x)).all() for x in leaves))
            self.assertTrue(any(np.any(np.asarray(x)!=0) for x in leaves))
            self.assertEqual(float(grad['bias'][1000+ord('A')]),0.)
        self.assertIs(common.compute_per_token_logps,original)

if __name__=='__main__': unittest.main()
