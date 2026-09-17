"""Evaluation-only JSON/key grammar for the pinned Tunix vanilla sampler.

Masks logits before selection. Bounds, integer constraints, duplicate keys and
truncation remain subject to validation; no response is repaired or retried.
"""
import json
import re


class KeyGrammar:
    def __init__(self, names):
        names = tuple(sorted(names))
        if not names or any(not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', n) for n in names):
            raise ValueError('Expected nonempty ASCII parameter names')
        self.names = names
        self.edges = [{} for _ in range(14)]
        # 0 start, 1 object, 2 key start, 3 colon, 4 number start,
        # 5 minus, 6 zero, 7 integer, 8 dot, 9 fraction,
        # 10 exponent, 11 exponent sign, 12 exponent digits, 13 complete.
        e = self.edges
        e[0]['{'] = 1; e[1]['}'] = 13
        e[1]['"'] = e[2]['"'] = self.new_state()
        trie = e[1]['"']
        for name in names:
            state = trie
            for char in name:
                if char not in e[state]: e[state][char] = self.new_state()
                state = e[state][char]
            e[state]['"'] = 3
        e[3][':'] = 4; e[4]['-'] = 5
        for s in (4, 5):
            e[s]['0'] = 6
            for c in '123456789': e[s][c] = 7
        for s in (7, 8, 9, 10, 11, 12):
            for c in '0123456789': e[s][c] = {8:9,10:12,11:12}.get(s,s)
        for s in (6,7): e[s]['.'] = 8
        for s in (6,7,9):
            e[s]['e'] = e[s]['E'] = 10
        e[10]['+'] = e[10]['-'] = 11
        for s in (6,7,9,12):
            e[s][','] = 2; e[s]['}'] = 13
        for s in (0,1,2,3,4,13):
            for c in ' \n\r\t': e[s][c] = s
        # Whitespace after a number must not allow another digit.
        after = self.new_state()
        for c in ' \n\r\t':
            e[after][c] = after
            for s in (6,7,9,12): e[s][c] = after
        e[after][','] = 2; e[after]['}'] = 13

    def new_state(self):
        self.edges.append({})
        return len(self.edges)-1

    def advance(self, state, text):
        for c in text:
            state = self.edges[state].get(c, -1) if state >= 0 else -1
            if state < 0: break
        return state

    def compile(self, tokenizer):
        """Only literal ASCII pieces (including ASCII byte fallback)."""
        import numpy as np
        hf = getattr(tokenizer, 'tokenizer', tokenizer)
        vocab = hf.get_vocab()
        alphabet = set().union(*(set(e) for e in self.edges))
        pieces = []
        specials = set(hf.all_special_ids)
        for token, ident in vocab.items():
            piece = token.replace('▁', ' ')
            if re.fullmatch(r'<0x[0-9A-F]{2}>', token):
                piece = chr(int(token[3:5], 16))
            if ident not in specials and piece and set(piece) <= alphabet:
                # Reject tokenizers whose isolated decoding disagrees with pieces.
                if hf.decode([ident], clean_up_tokenization_spaces=False) == piece:
                    pieces.append((ident, piece))
        pieces.sort()
        ids = [i for i, _ in pieces]
        stop = hf.convert_tokens_to_ids('<end_of_turn>')
        if stop != 106 or hf.decode([stop]) != '<end_of_turn>':
            raise ValueError('Pinned Gemma stop token missing')
        ids.append(stop)
        transitions = np.full((len(self.edges),len(ids)+1), -1, dtype=np.int32)
        for s in range(len(self.edges)):
            for j, (_, piece) in enumerate(pieces): transitions[s,j] = self.advance(s,piece)
        transitions[13,len(ids)-1] = 13
        lookup = np.full(max(vocab.values())+1,len(ids),dtype=np.int32)
        lookup[ids] = np.arange(len(ids))
        # Check that every grammar edge can be emitted (prevents dead ends).
        singles = {piece for _,piece in pieces if len(piece)==1}
        if not alphabet <= singles: raise ValueError('Tokenizer cannot cover JSON grammar')
        return np.array(ids,dtype=np.int32), transitions, lookup

    def validate(self, text):
        if self.advance(0,text) != 13: raise ValueError('Incomplete or nonconforming constrained response')
        def unique(pairs):
            out = {}
            for k,v in pairs:
                if k in out: raise ValueError('Duplicate parameter key')
                out[k]=v
            return out
        obj=json.loads(text,object_pairs_hook=unique)
        if not isinstance(obj,dict) or set(obj)-set(self.names): raise ValueError('Unknown key')
        return obj


def make_sampler(original, grammar, tokenizer):
    """Create a fresh sampler so its JIT closures bind the constrained subclass."""
    import jax
    import jax.numpy as jnp
    from tunix.generate.sampler import Sampler
    ids, transitions, lookup = grammar.compile(tokenizer)

    class ConstrainedSampler(Sampler):
        def _sample(self, logits, eos, cache, sampler_state):
            if sampler_state.sampling_mode == 'beam_search':
                raise ValueError('Constrained beam search is unsupported')
            table = jnp.asarray(transitions)
            mapping = jnp.asarray(lookup)
            def advance(i, states):
                tokens = sampler_state.token_buffer[:,i]
                cols = mapping[jnp.clip(tokens,0,len(lookup)-1)]
                nxt = table[jnp.maximum(states,0),cols]
                return jnp.where(states < 0,-1,nxt)
            states = jax.lax.fori_loop(sampler_state.num_input_tokens,
                sampler_state.decoding_step+1,advance,
                jnp.zeros(logits.shape[0],dtype=jnp.int32))
            legal = (table[jnp.maximum(states,0),:-1] >= 0) & (states[:,None]>=0)
            # Already-finished rows may emit only the stop token.
            legal = jnp.where(sampler_state.done[:,None],jnp.asarray(ids)[None,:]==ids[-1],legal)
            selected = logits[:,-1,jnp.asarray(ids)]
            masked = jnp.full_like(logits[:,-1,:],-jnp.inf)
            masked = masked.at[:,jnp.asarray(ids)].set(jnp.where(legal,selected,-jnp.inf))
            return super()._sample(masked[:,None,:],eos,cache,sampler_state)
    result=ConstrainedSampler(original.transformer,original.tokenizer,original.cache_config)
    return result


def install(rollout, tokenizer, names):
    grammar=KeyGrammar(names)
    rollout._sampler=make_sampler(rollout._sampler,grammar,tokenizer)
    return grammar
