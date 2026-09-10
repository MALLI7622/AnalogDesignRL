# Task generator checks — 2026-09-08

This records the API/replay implementation before the Codex-login provider was added. Hashes refer to that recorded implementation.

The generator is implemented for OpenAI, Gemini, DeepSeek, Z.AI/GLM, and OpenAI-compatible servers. [Usage and configuration](../generation/README.md). Model IDs remain configurable.

**55 unit tests passed**, including the existing 24 verifier/episode tests. New checks cover provider request formats, credential handling, bounded retries, incomplete responses, malformed proposals, false citations, parameter limits, fixed requirements, duplicate tasks, revision feedback, and output packaging.

Both circuit templates completed an offline replay through the real ngspice evaluator:

| Circuit | Reference | Valid starting design |
|---|---|---|
| AutoCkt SKY130 adaptation | All eight requirements pass | Phase margin 57.097° fails the 60° minimum |
| Fan SMC | All eight requirements pass | Unity frequency 0.669 MHz fails the 1.5 MHz minimum |

Raw requests, proposals, testbenches, and results are under `runs/generation_final/`. [task_generation.json](task_generation.json) records the results and hashes. Request previews for all five provider configurations also succeeded with text extracted from the local AutoCkt PDF.

**No live model API calls were tested.** Provider transport tests use mocked HTTP responses, and simulator checks use explicitly labeled, manually authored replay fixtures. These results establish pipeline behavior, not AI task-generation quality. A live batch requires a model ID and credentials for the selected endpoint.

This implementation generates sizing tasks for the two existing circuit templates. It does not automatically reconstruct new schematic images. Accepted tasks retain the existing training-review gate; split assignment and task-difficulty assessment remain pending.
