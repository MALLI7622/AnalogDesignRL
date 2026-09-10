"""Small HTTP adapters; model names are supplied by the caller, never inferred."""
from dataclasses import asdict, dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PRESETS = {
    "openai": ("responses", "https://api.openai.com/v1", "OPENAI_API_KEY", "schema"),
    "gemini": ("gemini", "https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY", "schema"),
    "deepseek": ("chat", "https://api.deepseek.com", "DEEPSEEK_API_KEY", "object"),
    "zai": ("chat", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY", "object"),
    "openai-compatible": ("chat", None, None, "prompt"),
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Invalid JSON number: {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON number is outside the finite float range.")
        return number

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)


class ProviderError(RuntimeError):
    """Configuration, transport, or incomplete generation; never a valid proposal."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


@dataclass
class ModelConfig:
    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    json_mode: str | None = None
    max_output_tokens: int = 8192
    timeout_s: float = 120
    retries: int = 2
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.provider not in PRESETS:
            raise ValueError(f"Unknown provider: {self.provider}")
        if not isinstance(self.model, str) or not self.model.strip() or any(ord(c) < 32 for c in self.model):
            raise ValueError("Supply the exact model ID available at your endpoint.")
        if self.model in {"REPLACE_WITH_MODEL_ID", "YOUR_MODEL_ID", "YOUR_SERVED_MODEL_ID"}:
            raise ValueError("Replace the example model placeholder with your endpoint's exact model ID.")
        _, base, key, mode = PRESETS[self.provider]
        self.base_url = self.base_url or base
        self.api_key_env = self.api_key_env or key
        self.json_mode = self.json_mode or mode
        if not isinstance(self.base_url, str):
            raise ValueError("openai-compatible requires base_url (including /v1 when appropriate).")
        parsed = urlsplit(self.base_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must be a server URL without credentials, query, or fragment.")
        local = parsed.hostname == "localhost"
        try:
            local = local or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("Use HTTPS, or HTTP on a loopback address for a local server.")
        self.base_url = self.base_url.rstrip("/")
        if self.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must name an environment variable, not contain an API key.")
        if self.json_mode not in {"schema", "object", "prompt"}:
            raise ValueError("json_mode must be schema, object, or prompt.")
        for name, value, lo, hi in (("max_output_tokens", self.max_output_tokens, 1, 131072),
                                    ("retries", self.retries, 0, 3)):
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer between {lo} and {hi}.")
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)) or not math.isfinite(self.timeout_s) or not 0 < self.timeout_s <= 600:
            raise ValueError("timeout_s must be finite, positive, and at most 600.")
        allowed = {
            "responses": {"temperature", "top_p", "reasoning"},
            "chat": {"temperature", "top_p", "seed", "thinking", "reasoning_effort", "chat_template_kwargs"},
            "gemini": {"temperature", "topP", "topK", "seed", "thinkingConfig"},
        }[self.protocol]
        if not isinstance(self.options, dict) or set(self.options) - allowed:
            raise ValueError(f"options may contain only: {', '.join(sorted(allowed))}")
        json.dumps(self.options, allow_nan=False)

    @property
    def protocol(self):
        return PRESETS[self.provider][0]

    def public(self):
        # Configuration contains the environment variable NAME, never its value.
        return asdict(self)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward credentials or paper contents to a different endpoint.
        return None


class ModelClient:
    mode = "live_model"

    def __init__(self, config):
        self.config = config

    def describe(self):
        return self.config.public()

    def check_credentials(self):
        name = self.config.api_key_env
        if name and not os.environ.get(name):
            raise ProviderError(f"Set {name} in your environment before generating tasks.")

    def prepare(self, system, messages, schema):
        cfg = self.config
        if cfg.protocol == "responses":
            body = {"model": cfg.model, "instructions": system, "input": messages,
                    "max_output_tokens": cfg.max_output_tokens, "store": False, **cfg.options}
            if cfg.json_mode == "schema":
                body["text"] = {"format": {"type": "json_schema", "name": "analog_task",
                                           "strict": True, "schema": schema}}
            elif cfg.json_mode == "object":
                body["text"] = {"format": {"type": "json_object"}}
            url = cfg.base_url + "/responses"
        elif cfg.protocol == "gemini":
            generation = {"maxOutputTokens": cfg.max_output_tokens, **cfg.options}
            if cfg.json_mode != "prompt":
                generation["responseMimeType"] = "application/json"
            if cfg.json_mode == "schema":
                # Gemini's documented subset omits string-length constraints.
                # Keep those checks in the local validator for every provider.
                def gemini_schema(value):
                    if isinstance(value, dict):
                        return {k: gemini_schema(v) for k, v in value.items() if k not in {"minLength", "maxLength"}}
                    if isinstance(value, list):
                        return [gemini_schema(v) for v in value]
                    return value
                generation["responseJsonSchema"] = gemini_schema(schema)
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "model" if m["role"] == "assistant" else "user",
                                  "parts": [{"text": m["content"]}]} for m in messages],
                    "generationConfig": generation}
            model = cfg.model.removeprefix("models/")
            url = cfg.base_url + "/models/" + quote(model, safe="") + ":generateContent"
        else:
            body = {"model": cfg.model, "messages": [{"role": "system", "content": system}, *messages],
                    "max_tokens": cfg.max_output_tokens, "stream": False, **cfg.options}
            if cfg.json_mode == "schema":
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "analog_task", "strict": True, "schema": schema}}
            elif cfg.json_mode == "object":
                body["response_format"] = {"type": "json_object"}
            url = cfg.base_url + "/chat/completions"
        return {"url": url, "body": body}

    def complete(self, request):
        self.check_credentials()
        key = os.environ.get(self.config.api_key_env, "") if self.config.api_key_env else ""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            if self.config.protocol == "gemini":
                headers["x-goog-api-key"] = key
            else:
                headers["Authorization"] = "Bearer " + key
        body = json.dumps(request["body"], allow_nan=False).encode()
        opener = build_opener(NoRedirect())
        for attempt in range(self.config.retries + 1):
            try:
                req = Request(request["url"], data=body, headers=headers, method="POST")
                with opener.open(req, timeout=self.config.timeout_s) as response:
                    content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ProviderError("Provider response exceeded 8 MiB.")
                decoded = content.decode("utf-8")
                # Never persist a credential echoed by an upstream error or proxy.
                if key:
                    decoded = decoded.replace(key, "[REDACTED]")
                raw = strict_json(decoded)
                reply = extract_reply(self.config.protocol, raw)
                reply["http_attempts"] = attempt + 1
                return reply
            except HTTPError as exc:
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                error_response = None
                message = "check endpoint, model ID, credentials, and JSON mode"
                try:
                    error_text = exc.read(8192).decode("utf-8", errors="replace")
                    if key:
                        error_text = error_text.replace(key, "[REDACTED]")
                    error_response = strict_json(error_text)
                    detail = error_response.get("error", {}) if isinstance(error_response, dict) else {}
                    detail = detail.get("message") if isinstance(detail, dict) else detail
                    if isinstance(detail, str) and detail.strip():
                        message = detail[:500]
                except (ValueError, OSError):
                    pass
                exc.close()
                if not retryable or attempt == self.config.retries:
                    raise ProviderError(f"Provider returned HTTP {exc.code}: {message}", error_response) from None
            except (URLError, TimeoutError, OSError):
                if attempt == self.config.retries:
                    raise ProviderError("Provider connection failed or timed out.") from None
            except (ValueError, UnicodeError):
                raise ProviderError("Provider returned an invalid JSON response.") from None
            time.sleep(min(2 ** attempt, 4))
        raise AssertionError("Unreachable retry state")


def extract_reply(protocol, raw):
    """Only final, complete text is accepted; reasoning/tool output is excluded."""
    try:
        if protocol == "responses":
            if raw.get("status") != "completed":
                raise ProviderError("OpenAI response was incomplete or failed; inspect token limit and response.", raw)
            parts = [part for item in raw["output"] if item.get("type") == "message"
                     for part in item.get("content", [])]
            if any(part.get("type") == "refusal" for part in parts):
                raise ProviderError("The model declined the generation request.", raw)
            text = "".join(p["text"] for p in parts if p.get("type") == "output_text")
            usage = raw.get("usage", {})
        elif protocol == "gemini":
            candidates = raw.get("candidates", [])
            if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
                raise ProviderError("Gemini returned no complete candidate; inspect finish reason and token limit.", raw)
            parts = candidates[0]["content"]["parts"]
            text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
            usage = raw.get("usageMetadata", {})
        else:
            choices = raw.get("choices", [])
            if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
                raise ProviderError("Chat completion was truncated, blocked, or requested a tool.", raw)
            message = choices[0]["message"]
            if message.get("refusal") or message.get("tool_calls"):
                raise ProviderError("Expected a task proposal, received a refusal or tool call.", raw)
            text = message.get("content")
            usage = raw.get("usage", {})
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Provider returned no final text.", raw)
        return {"text": text, "raw": raw, "usage": usage}
    except (KeyError, TypeError, AttributeError):
        raise ProviderError("Unrecognized provider response shape.", raw) from None


class ReplayClient:
    """Offline integration checks, explicitly recorded as replay rather than AI calls."""
    mode = "replay"

    def __init__(self, path):
        self.path = Path(path)
        data = strict_json(self.path.read_text())
        self.replies = data if isinstance(data, list) else [data]
        self.index = 0

    def describe(self):
        from .simulator import digest
        return {"provider": "replay", "model": "recorded-proposals", "file": self.path.name,
                "sha256": digest(self.path)}

    def check_credentials(self):
        pass

    def prepare(self, system, messages, schema):
        return {"url": "replay://recorded-proposals", "body": {
            "system": system, "messages": messages, "schema": schema}}

    def complete(self, request):
        if self.index == len(self.replies):
            raise ProviderError("Replay exhausted its recorded proposals.")
        item = self.replies[self.index]
        self.index += 1
        return {"text": item if isinstance(item, str) else json.dumps(item, allow_nan=False),
                "raw": item, "usage": {}, "http_attempts": 0}
