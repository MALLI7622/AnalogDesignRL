"""Pinned Gemma chat format shared by rollout and incremental trajectory masking."""
import copy


class PromptWindowExceeded(RuntimeError):
    pass


class GemmaChatParser:
    assistant_token = "<start_of_turn>model\n"

    def __init__(self, tokenizer, max_prompt_tokens=None):
        self.tokenizer = tokenizer
        self.max_prompt_tokens = max_prompt_tokens

    def preprocess_messages(self, messages):
        # Tunix calls this before converting individual messages into loss masks.
        # Never alter the agent's history (the old parser mutated its first user).
        result = copy.deepcopy(messages)
        if result and result[0]['role'] == 'system':
            system = result.pop(0)['content']
            if not result or result[0]['role'] != 'user':
                raise ValueError('Gemma requires a user message after the system instruction')
            result[0]['content'] = system + '\n\n' + result[0]['content'].strip()
        return result

    def parse(self, messages, add_generation_prompt=False, is_first_msg=False):
        messages = copy.deepcopy(messages)
        if is_first_msg:
            result = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=add_generation_prompt)
        else:
            if len(messages) != 1 or messages[0]['role'] not in {'user', 'assistant'}:
                raise ValueError('Incremental Gemma rendering requires one user or assistant message')
            if messages[0]['role'] == 'user':
                standalone = self.tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=add_generation_prompt)
                bos = self.tokenizer.bos_token
                if not standalone.startswith(bos):
                    raise ValueError('Gemma template must emit BOS')
                # Pinned vanilla Sampler excludes the stop token from returned
                # completions. Restore the whole turn boundary as masked context.
                result = '<end_of_turn>\n' + standalone[len(bos):]
            else:
                dummy = {'role': 'user', 'content': 'template prefix'}
                prefix = self.tokenizer.apply_chat_template([dummy], tokenize=False, add_generation_prompt=False)
                complete = self.tokenizer.apply_chat_template(
                    [dummy, messages[0]], tokenize=False, add_generation_prompt=add_generation_prompt)
                if not complete.startswith(prefix):
                    raise ValueError('Gemma template prefix is inconsistent')
                result = complete[len(prefix):]
        if self.max_prompt_tokens is not None:
            count = len(self.tokenizer.encode(result, add_special_tokens=False))
            if count > self.max_prompt_tokens:
                raise PromptWindowExceeded('Conversation exceeds the configured prompt bucket')
        return result


def verify_chat_contract(tokenizer, parser=None):
    """Cheap startup gate: no weights, inference, or accelerator initialization."""
    from tunix.rl.agentic.utils import tokenize_and_generate_masks
    parser = parser or GemmaChatParser(tokenizer)
    reference = getattr(tokenizer, 'tokenizer', tokenizer)
    messages = [{'role': 'system', 'content': 'Return parameter JSON.'},
                {'role': 'user', 'content': 'C=5; allowed range [1, 10].'},
                {'role': 'assistant', 'content': '{"C":6}'},
                {'role': 'user', 'content': 'Gain failed; current C=6.'}]
    before = copy.deepcopy(messages)
    actual = parser.parse(messages, is_first_msg=True, add_generation_prompt=True)
    expected = reference.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    if actual != expected or messages != before:
        raise ValueError('Chat format does not match pinned tokenizer or mutated history')
    initial, initial_masks = tokenize_and_generate_masks(messages[:2], tokenizer, parser,
        contains_first_msg=True, contains_generation_msg=True)
    from tunix.generate.utils import np_find_first_eos_idx
    import numpy as np
    sampled = np.array(tokenizer.encode('{"C":6}<end_of_turn>', add_special_tokens=False))
    generated = sampled[:np_find_first_eos_idx(sampled, np.array([1, 106]))].tolist()
    feedback, feedback_masks = tokenize_and_generate_masks(messages[3:], tokenizer, parser,
        contains_generation_msg=True)
    if initial + generated + feedback != tokenizer.encode(expected, add_special_tokens=False):
        raise ValueError('Incremental training tokens differ from the inference prompt')
    if any(initial_masks) or any(feedback_masks) or messages != before:
        raise ValueError('Prompt/feedback mask or history mutation is incorrect')
    return {'status': 'passed', 'format': 'pinned_huggingface_gemma_template_v1',
            'full_and_incremental_tokens_match': True, 'feedback_mask_zero': True,
            'history_preserved': True, 'sampler_stop_token_excluded': True,
            'limitation': 'CPU check of pinned sampler slicing and synthetic completions; no model behavior tested.'}


def completion_alignment(tokenizer, prompts, output):
    """Check actual returned tokens against the canonical next-turn context."""
    from tunix.utils.token_sanitization import sanitize_control_tokens
    boundary = '<end_of_turn>\n<start_of_turn>user\nalignment probe<end_of_turn>\n<start_of_turn>model\n'
    checks = []
    if len(prompts) != len(output.text) or len(prompts) != len(output.tokens):
        raise ValueError('Completion/prompt batch size mismatch')
    for prompt, response, tokens in zip(prompts, output.text, output.tokens):
        rendered = prompt + sanitize_control_tokens(response).strip() + boundary
        expected = tokenizer.encode(rendered, add_special_tokens=False)
        assembled = (tokenizer.encode(prompt, add_special_tokens=False)
                     + [int(token) for token in tokens]
                     + tokenizer.encode(boundary, add_special_tokens=False))
        checks.append(assembled == expected)
    return {'valid': all(checks), 'per_completion': checks,
            'contract': 'pinned_vanilla_stop_excluded_canonical_continuation'}
