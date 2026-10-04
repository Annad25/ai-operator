"""OpenRouter client (OpenAI-compatible) with a disk cache, call counting and fallbacks.

The cache key is the full request (model + messages + tools). Identical requests
are served from disk, so re-running a scenario while developing costs nothing.
LangGraph also re-executes a node when resuming after an interrupt; the cache
makes that replay deterministic and free.
"""
import hashlib
import json
import time

from openai import OpenAI

from . import config

STATS = {"calls": 0, "cache_hits": 0}
_client = None


def _get_client():
    global _client
    if _client is None:
        if not config.OPENROUTER_API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
        _client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=config.OPENROUTER_API_KEY)
    return _client


def _cache_path(payload: dict):
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    d = config.CACHE_DIR / "llm"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def chat(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
         temperature: float = 0.0) -> dict:
    """Returns the assistant message as a plain dict: {role, content, tool_calls?}."""
    payload = {"model": config.MODEL, "messages": messages, "tools": tools, "json": json_mode, "t": temperature}
    path = _cache_path(payload)
    if config.LLM_CACHE and path.exists():
        STATS["cache_hits"] += 1
        return json.loads(path.read_text())

    models = [config.MODEL] + config.FALLBACK_MODELS
    last_err = None
    for model in models:
        for attempt in range(3):
            try:
                kwargs = dict(model=model, messages=messages, temperature=temperature)
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = _get_client().chat.completions.create(**kwargs)
                STATS["calls"] += 1
                msg = resp.choices[0].message
                out = {"role": "assistant", "content": msg.content or ""}
                if msg.tool_calls:
                    out["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                        for tc in msg.tool_calls
                    ]
                path.write_text(json.dumps(out))
                return out
            except Exception as e:  # rate limits, provider errors
                last_err = e
                status = getattr(e, "status_code", None)
                if status == 400 and json_mode:
                    json_mode = False  # model may not support response_format; the prompt still asks for JSON
                    continue
                if status in (400, 401, 402, 403):
                    break  # not retryable on this model
                time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM call failed on all models: {last_err}")


def chat_json(system: str, user: str) -> dict:
    msg = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)
    return parse_json(msg["content"])


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])
