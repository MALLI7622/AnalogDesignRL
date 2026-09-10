# AI task generation

An AI model reads supplied source material and proposes sizing tasks. Python validates each proposal, then ngspice checks its reference and starting design. Accepted task JSON files work with the existing evaluator and episode API. See the [first six-task batch](../reports/pilot_20260909.md) and [API/replay checks](../reports/task_generation.md).

Initial testing uses GPT-6 through your signed-in Codex account. Run from the repository root after `python3 scripts/setup.py`. If needed, run `codex login` and sign in with ChatGPT; no separate API key is required. Codex supports [saved-login automation and JSON schema output](https://learn.chatgpt.com/docs/non-interactive-mode).

```sh
python3 scripts/generate_tasks.py \
  --config generation/providers/codex.json \
  --template autockt_two_stage \
  --count 1 --max-attempts 3 \
  --output datasets/autockt_batch_001
```

| Service | `--provider` | Key environment variable | API used |
|---|---|---|---|
| Codex with ChatGPT login | `codex` | None | `codex exec` |
| OpenAI | `openai` | `OPENAI_API_KEY` | [Responses](https://developers.openai.com/api/docs/guides/structured-outputs) |
| Gemini | `gemini` | `GEMINI_API_KEY` | [generateContent](https://ai.google.dev/api/generate-content) |
| DeepSeek hosted API | `deepseek` | `DEEPSEEK_API_KEY` | [Chat Completions / JSON output](https://api-docs.deepseek.com/guides/json_mode/) |
| GLM through Z.AI | `zai` | `ZAI_API_KEY` | [Chat Completions](https://docs.z.ai/api-reference/llm/chat-completion) |
| Your model server | `openai-compatible` | Optional `--api-key-env VARIABLE_NAME` | OpenAI-compatible Chat Completions |

For an open-source GLM or DeepSeek deployment, run an inference server separately and supply its URL and served model ID:

```sh
python3 scripts/generate_tasks.py \
  --provider openai-compatible \
  --base-url http://127.0.0.1:8000/v1 --model YOUR_SERVED_MODEL_ID \
  --template fan_smc --count 10
```

Model IDs are passed unchanged. A name such as GLM-5.2 works only when the selected endpoint serves that model; this project does not download weights or assert model availability. HTTPS is required for remote endpoints; HTTP is allowed on loopback for local servers or tunnels.

Codex mode checks `codex login status`, uses `gpt-6-astra`, and runs one temporary worker per proposal. The worker has read-only access, tool features disabled, and no project checkout. Its final JSON goes through the same validator and simulator as every other provider. It uses your Codex account's model access and usage limits. API-key environment variables are excluded from the worker. Codex CLI 0.153.4 is the locally checked version; `--codex-bin` can select another executable.

Codex settings are in [providers/codex.json](providers/codex.json): `model`, `reasoning_effort`, and `timeout_s` (300 seconds by default). `--request-timeout` overrides that deadline; a timed-out worker is terminated. Codex mode does not accept HTTP-only options such as `--max-output-tokens`, `--retries`, or `--api-key-env`. The proposal count is bounded by `--max-attempts`.

For direct API providers, set the corresponding key environment variable. Configuration files are in [providers](providers). Edit a copy, or override its model on the command line:

```sh
python3 scripts/generate_tasks.py \
  --config generation/providers/gemini.json --model YOUR_MODEL_ID \
  --source external/AutoCkt-2001.01808v2.pdf \
  --template autockt_two_stage --count 1 --dry-run
```

`--dry-run` writes a request preview without using an API key or simulator. Remove it to generate. `--source` accepts local PDF/text files and can be repeated; the circuit's source notes, netlist, and parameters are always included. PDF text extraction requires Poppler's `pdftotext`. It does not interpret schematic images or scanned pages. Oversized source text is rejected rather than silently truncated.

OpenAI and Gemini default to JSON schema output; DeepSeek/Z.AI use JSON object output. Compatible servers default to a JSON instruction in the prompt. `--json-mode schema|object|prompt` overrides this when a model supports different formats. Every mode uses the same local validation. Optional provider settings, such as `reasoning` for OpenAI, `thinking` for chat providers, or `thinkingConfig` for Gemini, belong in the config's `options` object; defaults are left to the provider. Keys are read from environment variables and are not saved in request records.

The AI can change starting values, reference values, and targets. It must retain all eight metrics and may only keep or tighten the template's requirements. Circuit structure, allowed parameter ranges, test conditions, testbenches, and rewards stay fixed. Every accepted reference passes; every accepted starting design produces valid measurements and fails at least one requirement. A failed simulation, fabricated source quote, invalid parameter, or duplicate task is rejected. Feedback is returned to the model for another proposal, within `--max-attempts`.

Outputs default to a new, ignored `datasets/generated_*/` directory:

- `tasks/`: accepted executable tasks, without reference answers.
- `private/`: reference parameters, proposal rationale, assumptions, and measured results.
- `attempts/`: requests, replies, usage returned by the provider, rejection reasons, and raw simulations.
- `sources/`: exact text sent to the model; hashes and settings are recorded in `manifest.json`.

Output directories must be new. The default attempt limit is three times the requested count; each proposal uses at most four ngspice invocations. HTTP calls retry transient errors up to twice by default. Authentication, endpoint, format, and incomplete-response errors stop the batch with an error record. Exit codes are `0` for a complete batch or preview, `1` for an unfinished batch, and `2` for configuration/provider errors.

If a batch stops early, its accepted tasks remain saved. Request the missing count in a new directory and add `--exclude-batch PATH_TO_PREVIOUS_BATCH`; the generator checks the earlier task hashes, shows those tasks to the model, and rejects duplicates before simulation. The flag can be repeated for batches using the same template. For a Codex timeout, also consider `--request-timeout 600`.

Check the whole pipeline without calling a model:

```sh
python3 scripts/generate_tasks.py \
  --replay generation/examples/autockt_reply.json \
  --template autockt_two_stage --count 1
```

Replay inputs are manually authored fixtures and are labeled `generation_mode: replay`. They are not evidence of model performance.

This version generates sizing tasks for the two supported circuit templates. Adding a new paper's topology still requires a compatible netlist and verified testbench. Quote matching checks that cited text exists; it does not verify the model's interpretation. Acceptance checks feasibility, not task difficulty or diversity. Accepted tasks remain unapproved for training. `requirement_group` groups identical requirements across different starts to help avoid split leakage; splits are not assigned automatically. Give a future learner only the evaluator API—folder separation alone does not protect answers.
