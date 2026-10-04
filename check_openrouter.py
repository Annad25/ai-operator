"""Check the OpenRouter key, list free models that support tool calling, and test one.

  python check_openrouter.py                 # key status + free tool-calling models
  python check_openrouter.py <model-id>      # also send one tool-calling test request (uses 1 request)

Reads OPENROUTER_API_KEY from .env in the current folder.
"""
import json
import sys

import httpx
from dotenv import dotenv_values

key = dotenv_values(".env").get("OPENROUTER_API_KEY", "")
if not key:
    sys.exit("No OPENROUTER_API_KEY in .env")
H = {"Authorization": f"Bearer {key}"}

# 1. Key status (does not use quota)
r = httpx.get("https://openrouter.ai/api/v1/key", headers=H, timeout=20)
if r.status_code != 200:
    sys.exit(f"Key check failed ({r.status_code}): {r.text[:200]}")
d = r.json()["data"]
print(f"Key OK. Free tier: {d.get('is_free_tier')}  Usage: ${d.get('usage', 0):.4f}  Limit: {d.get('limit')}")

# 2. Free models that accept tools (public endpoint)
models = httpx.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]
free = [m for m in models if m["id"].endswith(":free") and "tools" in (m.get("supported_parameters") or [])]
free.sort(key=lambda m: -(m.get("context_length") or 0))
print(f"\n{len(free)} free models with tool calling:")
for m in free:
    print(f"  {m['id']:<55} context {m.get('context_length')}")

# 3. Optional: one real tool-calling request
if len(sys.argv) > 1:
    model = sys.argv[1]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "What is the weather in Pune? Use the tool."}],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "Get weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
    }
    r = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=H, json=body, timeout=90)
    if r.status_code != 200:
        sys.exit(f"\n{model} failed ({r.status_code}): {r.text[:300]}")
    msg = r.json()["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if calls:
        print(f"\n{model}: tool call OK -> {calls[0]['function']['name']}({calls[0]['function']['arguments']})")
    else:
        print(f"\n{model}: answered WITHOUT calling the tool. Pick another model.\n{json.dumps(msg)[:300]}")
