"""Deterministic policy gate.

The LLM is told about company policy, but it is not trusted to enforce it.
Before any action runs, it is checked against company/rules.yaml in code.
"""
import operator
import re

from .knowledge import rules
from .tools.browser import BROWSER

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}


def _target(tool: str, args: dict, target: dict | None) -> dict:
    if target:
        return target
    if tool.startswith("browser_") and "ref" in args:
        return BROWSER.element(args["ref"]) or {}
    return {}


def evaluate(tool: str, args: dict, facts: dict, target: dict | None = None) -> dict | None:
    """Returns the first matching rule decision, or None if the action is allowed."""
    url = BROWSER.last.get("url", "")
    el = _target(tool, args, target)
    name, role = el.get("name", ""), el.get("role", "")
    for r in rules():
        w = r.get("when", {})
        if w.get("tool") and w["tool"] != tool:
            continue
        if w.get("url_contains") and w["url_contains"] not in url:
            continue
        if w.get("element_name_regex") and not re.search(w["element_name_regex"], name or ""):
            continue
        if w.get("element_role") and w["element_role"] != role:
            continue
        if any(not re.search(rx, str(args.get(k, ""))) for k, rx in (w.get("arg_regex") or {}).items()):
            continue
        cond = r.get("condition")
        if cond:
            val = facts.get(cond["fact"])
            if val is None:
                # Unknown value on a guarded action: fail safe, ask a human.
                return {"rule": r["id"], "effect": "require_approval", "approver": r.get("approver", "a human"),
                        "reason": f"{r['description']} ({cond['fact']} is unknown)"}
            try:
                if not OPS[cond["op"]](float(val), float(cond["value"])):
                    continue
            except (TypeError, ValueError):
                pass
        return {"rule": r["id"], "effect": r["effect"], "approver": r.get("approver", "a human"),
                "reason": r["description"], "element": name}
    return None


def approval_key(decision: dict, facts: dict) -> str:
    """Same rule + same facts = same approval, so a retried click is not re-asked."""
    relevant = {k: facts.get(k) for k in ("invoice_no", "amount", "patient_id", "claim_no", "sender_email")}
    return f"{decision['rule']}|{sorted(relevant.items())}"
