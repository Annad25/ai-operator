"""Procedure memory: learn a task once, replay it cheaply afterwards.

After a run is VERIFIED, its action trace is compiled into a parameterized
procedure and stored per intent:
  - browser element refs are replaced by (role, accessible name)
  - argument values that equal run facts become {{fact}} placeholders
  - values that came from an earlier step's output become {{s<i>.<path>}}

Replay executes the steps without the planner. LLM calls happen only where a
step needs one (extraction) or when a step breaks:
  - element not found (UI drift)  -> one LLM call to re-locate it, procedure patched
  - transient error (HTTP 5xx)    -> retry with backoff
  - tool flags a decision point   -> stop and hand over to the agent
  - anything else unexpected      -> hand over to the agent from the current state
Prior art: browser-use/workflow-use, OpenAdapt.
"""
import json
import re
import sqlite3
import time

from . import llm
from .config import PROCEDURE_DB
from .tools.browser import BROWSER

SKIP_TOOLS = {"ask_human", "finish", "browser_snapshot", "read_document", "remember"}
PH = re.compile(r"\{\{([^}]+)\}\}")


# ---------------- storage ----------------
def _db():
    con = sqlite3.connect(PROCEDURE_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS procedures(intent TEXT PRIMARY KEY, steps TEXT,
                   version INTEGER, runs INTEGER, heals INTEGER, updated_at TEXT)""")
    return con


def load(intent: str) -> dict | None:
    r = _db().execute("SELECT steps, version, runs, heals FROM procedures WHERE intent=?", (intent,)).fetchone()
    return {"intent": intent, "steps": json.loads(r[0]), "version": r[1], "runs": r[2], "heals": r[3]} if r else None


def save(intent: str, steps: list[dict], healed: bool = False):
    con = _db()
    old = load(intent)
    version = (old["version"] + 1) if old else 1
    con.execute("INSERT OR REPLACE INTO procedures VALUES (?,?,?,?,?,datetime('now'))",
                (intent, json.dumps(steps), version, (old["runs"] if old else 0) + 1,
                 (old["heals"] if old else 0) + int(healed)))
    con.commit()
    return version


def mark_run(intent: str):
    con = _db()
    con.execute("UPDATE procedures SET runs = runs + 1 WHERE intent=?", (intent,))
    con.commit()


# ---------------- compile ----------------
def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}{i}.")
    else:
        yield prefix.rstrip("."), obj


def _same(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def _templatize(value, facts: dict, prior: list[dict], partial_keys: tuple = ()):
    if not isinstance(value, (str, int, float)):
        return value
    for k, v in facts.items():                       # exact fact match
        if _same(value, v):
            return "{{" + k + "}}"
    for i, out in enumerate(prior):                  # value produced by an earlier step
        for path, v in _flatten(out):
            if _same(value, v):
                return "{{s" + str(i) + "." + path + "}}"
    if isinstance(value, str):                       # fact embedded in a longer string
        s = value
        for k, v in sorted(facts.items(), key=lambda kv: -len(str(kv[1]))):
            if not isinstance(v, str) or len(v) < 3:
                continue
            words = v.split()
            # Full value anywhere in the string. For request entities (e.g. vendor "MedSupply Co"),
            # also accept a shorter word-prefix, since people and models abbreviate names in search queries.
            lengths = range(len(words), 0, -1) if k in partial_keys else [len(words)]
            for n in lengths:
                part = " ".join(words[:n])
                if len(part) >= 3 and re.search(rf"\b{re.escape(part)}\b", s, re.I):
                    s = re.sub(rf"\b{re.escape(part)}\b", "{{" + k + "}}", s, flags=re.I)
                    break
        return s
    return value


def _templatize_hint(spec: str, facts: dict) -> str:
    if "(" not in spec:
        return spec
    key, hint = spec.split("(", 1)
    for k, v in facts.items():
        if isinstance(v, str) and len(v) >= 3 and v in hint:
            hint = hint.replace(v, "{{" + k + "}}")
    return key + "(" + hint


def _url_pattern(url: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
    return re.sub(r"\d+", r"\\d+", path)


def compile_trace(trace: list[dict], facts: dict, entity_keys: tuple = ()) -> list[dict]:
    steps, outputs = [], []
    for t in trace:
        if t["tool"] in SKIP_TOOLS:
            continue
        args = {k: v for k, v in t["args"].items() if k != "ref"}
        if isinstance(args.get("fields"), list):
            args_t = dict(args)
            args_t["path"] = _templatize(args["path"], facts, outputs, entity_keys)
            # hints like "followup_days (for Orthopedics)" must follow the new run's department
            args_t["fields"] = [_templatize_hint(f, facts) for f in args["fields"]]
        else:
            args_t = {k: _templatize(v, facts, outputs, entity_keys) for k, v in args.items()}
        target = dict(t["target"]) if t.get("target") else None
        if target:  # a link named exactly after a fact (e.g. the invoice number) becomes {{invoice_no}}.
            # Exact matches only: a label like "Approved amount" must not become "{{decision}} amount".
            key = next((k for k, v in facts.items() if _same(target["name"], v)), None)
            if key:
                target["name"] = "{{" + key + "}}"
        steps.append({"tool": t["tool"], "args": args_t, "target": target,
                      "expect_url": _url_pattern(t["post_url"]) if t.get("post_url") else None})
        outputs.append(t.get("data") or {})
    return steps


# ---------------- replay ----------------
def resolve(template, facts: dict, outputs: list[dict]):
    if isinstance(template, list):
        return [resolve(t, facts, outputs) for t in template]
    if not isinstance(template, str):
        return template

    def lookup(key):
        if key.startswith("s") and "." in key and key[1:key.index(".")].isdigit():
            i, path = int(key[1:key.index(".")]), key[key.index(".") + 1:]
            return dict(_flatten(outputs[i])).get(path) if i < len(outputs) else None
        return facts.get(key)

    m = PH.fullmatch(template)
    if m:
        return lookup(m.group(1))
    return PH.sub(lambda m: str(lookup(m.group(1))), template)


def locate(step: dict, facts: dict | None = None, outputs: list | None = None) -> tuple[str | None, bool]:
    """Find the element for a browser step. Returns (ref, healed)."""
    tgt = {**step["target"], "name": str(resolve(step["target"]["name"], facts or {}, outputs or []))}
    el = BROWSER.find(tgt["role"], tgt["name"])
    if el:
        return el["ref"], False
    # UI drift: ask the model which element now plays this role (one call).
    snap = BROWSER.render()
    ans = llm.chat_json(
        "You repair broken UI automation steps. Given the step and the current page, return JSON "
        '{"ref": "<ref or null>", "reason": "..."}. Only pick an element that clearly serves the same purpose.',
        f"Step: {step['tool']} on {tgt['role']} named '{tgt['name']}' with args {step['args']}\n\nPage:\n{snap}")
    ref = ans.get("ref")
    if ref and BROWSER.element(ref):
        new = BROWSER.element(ref)
        if "{{" not in step["target"]["name"]:  # keep parameterized names as they are
            step["target"] = {"role": new["role"], "name": new["name"]}  # patch the procedure
        return ref, True
    return None, False


def backoff(attempt: int):
    time.sleep(min(0.5 * 2 ** attempt, 4))
