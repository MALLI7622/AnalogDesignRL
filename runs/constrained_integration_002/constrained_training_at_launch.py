"""Pinned Tunix integration: identical grammar distributions for sampling/GRPO.

Host-side schema/state extraction feeds explicit arrays to the actor loss and
reference/anchor inference. No grammar state is inferred inside autodiff.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, replace
import json
import numpy as np

from training.constrained_json import KeyGrammar, make_sampler

MARKER = 'Circuit task and feedback:\n'


def prompt_names(prompt):
    if MARKER not in prompt:
        raise ValueError('Initial public task specification missing from prompt')
    spec, _ = json.JSONDecoder().raw_decode(prompt.split(MARKER, 1)[1])
    if not isinstance(spec.get('parameters'), dict) or 'current_parameters' not in spec:
        raise ValueError('Initial parameter schema missing from prompt')
    return tuple(sorted(spec['parameters']))


class Constraints:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.cache = {}
        self.header = tokenizer.encode('<start_of_turn>model\n', add_special_tokens=False)

    def grammar(self, names):
        if names not in self.cache:
            g = KeyGrammar(names)
            self.cache[names] = (g, *g.compile(self.tokenizer))
        return self.cache[names]

    def metadata(self, prompts, completions, masks=None):
        prompts, completions = np.asarray(prompts), np.asarray(completions)
        specs = [self.grammar(prompt_names(self.tokenizer.decode(row.tolist()))) for row in prompts]
        batch, length = completions.shape
        width = max(len(s[1]) for s in specs)
        allowed = np.zeros((batch, length, width), dtype=bool)
        columns = np.zeros((batch, length), dtype=np.int32)
        active = np.zeros((batch, length), dtype=bool)
        token_ids = np.zeros((batch, width), dtype=np.int32)
        for b, (g, ids, table, lookup) in enumerate(specs):
            token_ids[b,:len(ids)] = ids
            state, generating, turn = 0, True, []
            tokens = completions[b].tolist()
            for i, token in enumerate(tokens):
                if token in (0,106):
                    if generating and turn:
                        g.validate(self.tokenizer.decode(turn))
                    generating, turn = False, []
                    continue
                if not generating:
                    if i+1 >= len(self.header) and tokens[i+1-len(self.header):i+1] == self.header:
                        generating, state = True, 0
                    continue
                if token < 0 or token >= len(lookup) or table[state,lookup[token]] < 0:
                    raise ValueError('Training sequence violates its task grammar')
                allowed[b,i,:len(ids)] = table[state,:-1] >= 0
                columns[b,i] = lookup[token]
                active[b,i] = True
                state = int(table[state,lookup[token]])
                turn.append(token)
            if generating and turn:
                g.validate(self.tokenizer.decode(turn))
        if masks is not None and np.any((np.asarray(masks)>0) != active):
            raise ValueError('Assistant grammar spans disagree with the training loss mask')
        return dict(allowed=allowed, columns=columns, active=active, token_ids=token_ids)


def masked_distribution(logits, metadata):
    """Logits already temperature-scaled. Non-assistant positions are neutral."""
    import jax
    import jax.numpy as jnp
    ids = jnp.asarray(metadata['token_ids'])
    selected = jnp.take_along_axis(logits.astype(jnp.float32), ids[:,None,:], axis=-1)
    allowed = jnp.asarray(metadata['allowed'])
    active = jnp.asarray(metadata['active'])
    # Finite sentinel has exactly zero probability in float32 and avoids
    # 0 * -inf in Tunix's entropy diagnostic and its gradients.
    selected = jnp.where(allowed, selected, -1e30)
    neutral = jnp.full_like(selected,-1e30).at[:,:,0].set(0.)
    selected = jnp.where(active[:,:,None],selected,neutral)
    logps = jax.nn.log_softmax(selected,axis=-1)
    chosen = jnp.take_along_axis(logps,jnp.asarray(metadata['columns'])[:,:,None],axis=-1)[:,:,0]
    return jnp.where(active,chosen,0.), selected


class ConstrainedGeneration:
    def __init__(self, rollout, constraints):
        self.rollout, self.constraints = rollout, constraints
        self.original = rollout.generate
        self.names = None

    def __call__(self, prompts, rollout_config, **kwargs):
        # SeededGeneration owns the shared sampler lock around this call.
        names = [prompt_names(p) for p in prompts]
        if any(n != names[0] for n in names):
            raise ValueError('Constrained sampler batches must share a task schema')
        if self.names != names[0]:
            grammar = self.constraints.grammar(names[0])[0]
            self.rollout._sampler = make_sampler(self.rollout._sampler,grammar,self.constraints.tokenizer)
            self.names = names[0]
        return self.original(prompts, replace(rollout_config, top_k=0, top_p=1.0), **kwargs)


@contextmanager
def training_probabilities(cluster, constraints, temperature):
    """Scoped dispatch for the three pinned Tunix likelihood callers.

    ContextVar keeps concurrent learner threads separate. Actual metadata is
    passed as dynamic JAX arrays, not captured as constants in a compiled loss.
    """
    import jax
    from flax import nnx
    from tunix.rl import common, algo_core
    from tunix.rl.agentic.agentic_grpo_learner import GRPOLearner
    import flax.struct

    original = common.compute_per_token_logps
    context = ContextVar('analog_constraint_metadata',default=None)

    @nnx.jit(static_argnames=('pad_id','eos_id','stop_gradient','return_logits','temperature'))
    def compute(graphdef,state,prompt_tokens,completion_tokens,pad_id,eos_id,metadata,
                images=None,stop_gradient=True,return_logits=False,
                segment_ids=None,segment_positions=None,temperature=1.0):
        if segment_ids is not None or images is not None:
            raise ValueError('Constrained training currently requires unpacked text sequences')
        _, logits = original(graphdef,state,prompt_tokens,completion_tokens,pad_id,eos_id,
            stop_gradient=stop_gradient,return_logits=True,temperature=1.0)
        # Match sampler.sample_top_p: upcast *before* temperature division.
        logits = logits.astype(jax.numpy.float32) / temperature
        logps, compact = masked_distribution(logits,metadata)
        if stop_gradient:
            logps, compact = jax.lax.stop_gradient(logps),jax.lax.stop_gradient(compact)
        return (logps,compact) if return_logits else logps

    def dispatch(*args,**kwargs):
        meta=context.get()
        if meta is None:
            raise RuntimeError('Constrained training likelihood called without grammar metadata')
        kwargs.setdefault('temperature',temperature)
        return compute(*args,metadata=meta,**kwargs)

    @flax.struct.dataclass
    class ConstrainedExample(common.TrainExample):
        constraint_metadata: object = None

    def constrained_loss(model, train_example, algo_config, pad_id, eos_id):
        token=context.set(train_example.constraint_metadata)
        try:
            return algo_core.grpo_loss_fn(model,train_example,algo_config,pad_id,eos_id)
        finally: context.reset(token)

    class Learner(GRPOLearner):
        example_type = ConstrainedExample
        constraint_loss = staticmethod(constrained_loss)
        def _process_results(self,*args,**kwargs):
            examples=super()._process_results(*args,**kwargs)
            return [ConstrainedExample(**{f.name:getattr(e,f.name) for f in fields(e)},
                constraint_metadata=jax.tree.map(jax.numpy.asarray,constraints.metadata(
                    e.prompt_ids,e.completion_ids,e.completion_mask))) for e in examples]

        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            def loss(model,train_example,algo_config):
                return constrained_loss(model,train_example,self.algo_config,
                    self.rl_cluster.rollout.pad_id(),self.rl_cluster.rollout.eos_id())
            self.rl_cluster.actor_trainer.with_loss_fn(loss,has_aux=True)

    saved={}
    for name in ('get_ref_per_token_logps','get_actor_per_token_logps','get_old_per_token_logps'):
        saved[name]=getattr(cluster,name)
        def wrapper(*args,_original=saved[name],**kwargs):
            prompts=kwargs.get('prompt_tokens',args[0] if args else None)
            completions=kwargs.get('completion_tokens',args[1] if len(args)>1 else None)
            token=context.set(jax.tree.map(jax.numpy.asarray,constraints.metadata(prompts,completions)))
            try: return _original(*args,**kwargs)
            finally: context.reset(token)
        setattr(cluster,name,wrapper)
    common.compute_per_token_logps=dispatch
    try:
        yield Learner
    finally:
        common.compute_per_token_logps=original
        for name,fn in saved.items(): setattr(cluster,name,fn)


def verify_rollout_probabilities(cluster, constraints, output, temperature):
    """Teacher-force the actual sampled tokens on device; never update weights."""
    import jax
    import jax.numpy as jnp
    from tunix.rl.agentic.agentic_grpo_learner import GRPOConfig
    from tunix.rl.rl_cluster import Role
    prompts=jnp.asarray(output.left_padded_prompt_tokens)
    width=max(map(len,output.tokens))
    completions=jnp.asarray([np.pad(t,(0,width-len(t))) for t in output.tokens])
    metadata=jax.tree.map(jnp.asarray,constraints.metadata(prompts,completions))
    sampled=jnp.asarray([np.pad(p,(0,width-len(p))) for p in output.logprobs])
    mask=metadata['active']
    pad_id, eos_id = cluster.rollout.pad_id(), cluster.rollout.eos_id()
    with training_probabilities(cluster,constraints,temperature) as Learner:
        print('Probability check: recomputing sampled-policy likelihoods', flush=True)
        old=cluster.get_old_per_token_logps(prompt_tokens=prompts,completion_tokens=completions)
        jax.block_until_ready(old)
        print('Probability check: reference likelihoods', flush=True)
        ref=cluster.get_ref_per_token_logps(prompt_tokens=prompts,completion_tokens=completions,pad_id=pad_id,eos_id=eos_id)
        example=Learner.example_type(prompt_ids=prompts,prompt_mask=prompts!=pad_id,
            completion_ids=completions,completion_mask=mask,advantages=jnp.zeros(prompts.shape[0]),
            ref_per_token_logps=ref,old_per_token_logps=sampled,constraint_metadata=metadata)
        config=GRPOConfig(num_generations=2,beta=.08)
        config.temperature=temperature
        # Actual installed GRPO loss, evaluated without autodiff or optimizer.
        print('Probability check: actor GRPO loss (no update)', flush=True)
        with cluster._get_mesh_and_logical_axis_rules_cm(Role.ACTOR):
            loss,aux=Learner.constraint_loss(cluster.actor_trainer.model,example,config,pad_id,eos_id)
    delta=np.abs(np.asarray(old-sampled))[np.asarray(mask)]
    result={'tokens_checked':int(mask.sum()),'max_abs_logp_error':float(delta.max()),
            'mean_abs_logp_error':float(delta.mean()),'grpo_loss':float(loss),
            'grpo_ratio_mean':float(aux['is_ratio/mean']),'grpo_kl':float(aux['kl']),
            'grpo_entropy':float(aux['entropy']),
            'reference_logps_finite':bool(np.isfinite(np.asarray(ref)[np.asarray(mask)]).all()),
            'max_error_limit':.1,'mean_error_limit':.01,'optimizer_updates':0}
    result['passed']=(result['max_abs_logp_error']<=.1 and result['mean_abs_logp_error']<=.01
        and result['reference_logps_finite'] and all(np.isfinite(result[k])
            for k in ('grpo_loss','grpo_ratio_mean','grpo_kl','grpo_entropy')))
    return result
