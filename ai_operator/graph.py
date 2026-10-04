"""The operator runtime.

    intake -> route -+-> replay ----------------------------+
                     |     | (needs approval) -> approve -+  |
                     |     | (stuck) -----+               |  |
                     |                    v               |  v
                     +-------------> agent -> gate -> tools -> verify -> report
                                       ^                 |        |
                                       +-----------------+        | (failed, retry)
                                       +--------------------------+

Goal -> Understand (intake) -> Plan/Execute/Observe/Adapt (agent loop or replay)
     -> Verify (independent check) -> Complete (report + learn procedure).

State is checkpointed to SQLite after every node, so a run can be resumed
after a crash and human approvals can pause a run indefinitely.
"""
import json
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import config, evidence, llm, policy, procedures
from .knowledge import load_sops, policies_text, search_sops
from .tools import registry
from .tools.browser import BROWSER
from .verifier import run_checks


class State(TypedDict, total=False):
    request: str
    run_id: str
    use_procedures: bool
    task: dict
    facts: dict
    messages: list
    pending: list
    decisions: dict
    approvals: list
    mode: str
    replay_idx: int
    replay_outputs: list
    healed: bool
    escalation: str
    trace: list
    turns: int
    verify_attempts: int
    finish: dict
    verification: dict
    status: str
    started_at: float
    pending_approval: Any


# ---------------- intake ----------------
def intake(s: State) -> dict:
    candidates = search_sops(s["request"])
    menu = "\n".join(f"- {c.intent}: {c.title} (required inputs: {c.inputs})" for c in candidates)
    system = ("You turn a staff request into a structured task for an AI operator at Sahyadri Care Hospital. "
              "Pick the matching intent or 'unknown'. Extract the required inputs exactly as written in the request. "
              "If a required input is missing or the request is ambiguous, set clarification to a short question; "
              'otherwise null. Reply with JSON: {"intent": str, "goal": str, "entities": {}, "clarification": str|null}')
    request = s["request"]
    task = llm.chat_json(system, f"Known procedures:\n{menu}\n\nRequest: {request}")
    if task.get("clarification"):
        answer = interrupt({"type": "question", "question": task["clarification"]})
        request = f"{request}\n(Clarification from requester: {answer})"
        task = llm.chat_json(system, f"Known procedures:\n{menu}\n\nRequest: {request}")
    evidence.log(s["run_id"], "intake", task=task)
    return {"task": task, "request": request, "facts": dict(task.get("entities") or {})}


def route(s: State) -> dict:
    intent = s["task"].get("intent")
    proc = procedures.load(intent) if s.get("use_procedures", True) else None
    mode = "replay" if proc else "agent"
    evidence.log(s["run_id"], "route", mode=mode, procedure_version=proc and proc["version"])
    return {"mode": mode, "replay_idx": 0, "replay_outputs": [], "trace": [], "turns": 0,
            "verify_attempts": 0, "approvals": [], "messages": [], "decisions": {}, "healed": False,
            "started_at": time.time() - 2}  # small allowance for clock rounding


# ---------------- agent loop ----------------
def _system_prompt(s: State) -> str:
    sop = load_sops().get(s["task"].get("intent"))
    sop_text = sop.body if sop else "No SOP matches this request. Work out the steps from the request and the apps."
    return f"""You are an AI operator at Sahyadri Care Hospital. You complete staff requests by operating company
systems with tools, not by describing what someone should do.

Request: {s['request']}
Goal: {s['task'].get('goal')}

Standard operating procedure:
{sop_text}

Company policies:
{policies_text()}

Company apps: ERP web app (start at "/"). Payables /payables (new: /payables/new), appointments
/appointments/new, insurance claims /claims, vendors /vendors. Mailbox (search_mail, send_mail) and the
shared drive (search_files) through their tools. The operator's own address is ai-operator@sahyadricare.example.

Working rules:
- Observe before acting. Use refs only from the most recent page snapshot.
- Batch independent actions in one turn (for example, fill every form field, then click save in the next turn).
- Get values from documents with extract_fields. Never type a value you did not observe.
- If an action fails, read the error on the page and adapt. Retry transient errors.
- Some actions need human approval; the runtime handles that. If an action is denied, do not work around it.
- Use ask_human only when the request is genuinely ambiguous.
- When finished, call finish. Your work will be verified independently against the system of record.

Run memory (facts observed so far): {json.dumps(s.get('facts', {}))}"""


def _compact(messages: list) -> list:
    """Keep only the latest page snapshot in full; older ones are summarized to save tokens."""
    last_snap = max((i for i, m in enumerate(messages) if m["role"] == "tool"
                     and m["content"].startswith(("URL:", "ACTION REACHED"))), default=-1)
    out = []
    for i, m in enumerate(messages):
        if m["role"] == "tool" and i != last_snap and m["content"].startswith(("URL:", "ACTION REACHED")):
            m = {**m, "content": m["content"].split("\n")[0] + " [older snapshot omitted]"}
        out.append(m)
    return out


def agent(s: State) -> dict:
    msgs = list(s.get("messages") or [])
    if not msgs:
        start = "Begin."
        if s.get("escalation"):
            start = (f"A saved procedure was replayed and stopped: {s['escalation']}\n"
                     f"Current page:\n{BROWSER.render()}\nContinue the task from here.")
        msgs.append({"role": "user", "content": start})
    reply = llm.chat([{"role": "system", "content": _system_prompt(s)}] + _compact(msgs), tools=registry.schemas())
    msgs.append(reply)
    calls = reply.get("tool_calls") or []
    if reply.get("content"):
        evidence.log(s["run_id"], "agent_thought", text=reply["content"])
    if not calls:
        msgs.append({"role": "user", "content": "Use a tool to continue, or call finish."})
    return {"messages": msgs, "pending": calls, "turns": s.get("turns", 0) + 1, "decisions": {}}


def _args(c) -> dict:
    try:
        return json.loads(c["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return {"_invalid_json": c["function"].get("arguments")}


def gate(s: State) -> dict:
    """Human-in-the-loop checkpoint. Runs BEFORE any pending action executes, so
    re-running this node on resume never repeats a side effect."""
    decisions, approvals = {}, list(s.get("approvals", []))
    for c in s.get("pending", []):
        name, args = c["function"]["name"], _args(c)
        if name == "ask_human":
            answer = interrupt({"type": "question", "question": args.get("question")})
            decisions[c["id"]] = {"answer": answer}
            continue
        d = policy.evaluate(name, args, s.get("facts", {}))
        if not d:
            continue
        if d["effect"] == "block":
            decisions[c["id"]] = {"denied": f"Blocked by {d['rule']}: {d['reason']}"}
            evidence.log(s["run_id"], "blocked", rule=d["rule"], action=name, element=d.get("element"))
        elif d["effect"] == "require_approval":
            key = policy.approval_key(d, s.get("facts", {}))
            if key in approvals:
                continue
            resp = interrupt({"type": "approval", "rule": d["rule"], "approver": d["approver"], "reason": d["reason"],
                              "action": f"{name} '{d.get('element', '')}'", "facts": s.get("facts", {})})
            if resp.get("approved"):
                approvals.append(key)
                evidence.log(s["run_id"], "approved", rule=d["rule"], by=d["approver"], note=resp.get("note"))
            else:
                decisions[c["id"]] = {"denied": f"{d['approver']} declined: {resp.get('note', 'no reason given')}"}
                evidence.log(s["run_id"], "denied", rule=d["rule"], note=resp.get("note"))
    return {"decisions": decisions, "approvals": approvals}


def _record(s_trace, s_facts, name, args, res, run_id):
    s_facts.update(res.facts)
    if res.ok:
        s_trace.append({"tool": name, "args": args, "target": res.target, "data": res.data,
                        "post_url": res.data.get("url") if name.startswith("browser_") else None})
    if registry.TOOLS[name].risk == "write" and name.startswith("browser_"):
        shot = evidence.run_dir(run_id) / "screens" / f"{len(s_trace):02d}_{name}.png"
        BROWSER.screenshot(shot)


def tools(s: State) -> dict:
    msgs, facts, trace = list(s["messages"]), dict(s.get("facts", {})), list(s.get("trace", []))
    finish, stop = None, False
    for c in s.get("pending", []):
        name, args = c["function"]["name"], _args(c)
        dec = s.get("decisions", {}).get(c["id"], {})
        if stop:
            content = "SKIPPED: an earlier action in this batch was denied or failed."
        elif "denied" in dec:
            content, stop = f"DENIED: {dec['denied']}", True
        elif name == "ask_human":
            content = f"Requester answered: {dec.get('answer')}"
        elif name == "finish":
            finish, content = args, "Submitted for verification."
        else:
            res = registry.execute(name, args, {"run_id": s["run_id"], "facts": facts})
            evidence.log(s["run_id"], "tool", tool=name, args=args, ok=res.ok)
            _record(trace, facts, name, args, res, s["run_id"])
            content = res.output[:3500]
            if not res.ok and name.startswith("browser_"):
                stop = True  # page state changed unexpectedly; let the model re-plan
        msgs.append({"role": "tool", "tool_call_id": c["id"], "content": content})
    return {"messages": msgs, "facts": facts, "trace": trace, "finish": finish, "pending": []}


# ---------------- replay ----------------
def replay(s: State) -> dict:
    proc = procedures.load(s["task"]["intent"])
    steps = proc["steps"]
    idx, outputs = s.get("replay_idx", 0), list(s.get("replay_outputs", []))
    facts, trace, healed = dict(s.get("facts", {})), list(s.get("trace", [])), s.get("healed", False)
    approvals = s.get("approvals", [])

    def stop(reason):
        evidence.log(s["run_id"], "replay_escalate", step=idx, reason=reason)
        return {"mode": "agent", "escalation": f"step {idx + 1} ({steps[idx]['tool']}): {reason}",
                "facts": facts, "trace": trace, "replay_idx": idx, "replay_outputs": outputs, "healed": healed}

    while idx < len(steps):
        step = steps[idx]
        args = {k: procedures.resolve(v, facts, outputs) for k, v in step["args"].items()}
        if step.get("target"):
            ref, did_heal = procedures.locate(step, facts, outputs)
            if not ref:
                return stop(f"element {step['target']} not found and could not be re-located")
            if did_heal:
                healed = True
                evidence.log(s["run_id"], "self_heal", step=idx, new_target=step["target"])
            args["ref"] = ref
        # every step goes through the same policy gate as the agent, not only browser clicks
        d = policy.evaluate(step["tool"], args, facts)
        if d and d["effect"] == "block":
            return stop(f"blocked by {d['rule']}")
        if d and d["effect"] == "require_approval" and policy.approval_key(d, facts) not in approvals:
            return {"mode": "approve", "facts": facts, "trace": trace, "replay_idx": idx,
                    "replay_outputs": outputs, "healed": healed, "pending_approval": d}
        res = None
        for attempt in range(3):
            res = registry.execute(step["tool"], args, {"run_id": s["run_id"], "facts": facts})
            if res.ok:
                break
            status = res.data.get("status") or 0
            if 400 <= status < 500:
                break  # business error (validation, duplicate): retrying will not help
            evidence.log(s["run_id"], "replay_retry", step=idx, attempt=attempt + 1, error=res.output[:150])
            procedures.backoff(attempt)
            if step.get("target"):
                ref, _ = procedures.locate(step, facts, outputs)
                if ref:
                    args["ref"] = ref
        evidence.log(s["run_id"], "replay_step", step=idx, tool=step["tool"], ok=res.ok)
        if not res.ok:
            return stop(res.output[:300])
        _record(trace, facts, step["tool"], args, res, s["run_id"])
        outputs.append(res.data)
        if res.needs_judgment:
            return stop(f"decision needed: {res.needs_judgment}")
        if step.get("expect_url"):
            path = re.sub(r"^https?://[^/]+", "", res.data.get("url", "")).split("?")[0]
            if not re.fullmatch(step["expect_url"], path):
                return stop(f"unexpected page {res.data.get('url')} (expected {step['expect_url']})")
        idx += 1
    return {"mode": "verify", "facts": facts, "trace": trace, "replay_idx": idx, "replay_outputs": outputs,
            "healed": healed, "finish": {"status": "done", "summary": "Completed by replaying a learned procedure."}}


def approve(s: State) -> dict:
    d = s["pending_approval"]
    resp = interrupt({"type": "approval", "rule": d["rule"], "approver": d["approver"], "reason": d["reason"],
                      "action": f"step {s.get('replay_idx', 0) + 1} of the saved procedure ({d.get('element') or 'send/submit'})",
                      "facts": s.get("facts", {})})
    if resp.get("approved"):
        evidence.log(s["run_id"], "approved", rule=d["rule"], by=d["approver"], note=resp.get("note"))
        return {"approvals": s.get("approvals", []) + [policy.approval_key(d, s["facts"])], "mode": "replay"}
    evidence.log(s["run_id"], "denied", rule=d["rule"], note=resp.get("note"))
    return {"mode": "report", "status": "denied",
            "finish": {"status": "blocked", "summary": f"{d['approver']} declined: {resp.get('note')}"}}


# ---------------- verify + report ----------------
def verify(s: State) -> dict:
    fin = s.get("finish") or {}
    if fin.get("status") == "blocked":
        return {"verification": {"passed": False, "details": ["Agent reported the task as blocked."]}, "status": "blocked"}
    sop = load_sops().get(s["task"].get("intent"))
    if sop and sop.kind == "answer":
        sources = sorted({f"{r['path']} (p.{r['page']})" for t in s.get("trace", []) if t["tool"] == "search_documents"
                          for r in t["data"].get("results", [])[:2]}
                         | {t["args"]["path"] for t in s.get("trace", []) if t["tool"] == "read_document"})
        result = {"passed": True, "unverifiable": False,
                  "details": ["Information request: nothing in company systems was changed.",
                              "Documents consulted: " + (", ".join(sources) or "none")]}
        evidence.log(s["run_id"], "verify", passed=True, details=result["details"])
        return {"verification": result, "status": "answered", "verify_attempts": 1}
    result = run_checks(sop.success_checks if sop else [], s.get("facts", {}), since=s.get("started_at"))
    evidence.log(s["run_id"], "verify", passed=result["passed"], details=result["details"])
    attempts = s.get("verify_attempts", 0) + 1
    if result["passed"]:
        return {"verification": result, "status": "verified", "verify_attempts": attempts}
    if result.get("unverifiable"):
        return {"verification": result, "status": "completed_unverified", "verify_attempts": attempts}
    if attempts >= config.MAX_VERIFY_ATTEMPTS:
        return {"verification": result, "status": "failed_verification", "verify_attempts": attempts}
    msgs = list(s.get("messages") or [])
    msgs.append({"role": "user", "content": "Independent verification FAILED:\n" + "\n".join(result["details"])
                 + "\nFix only what failed, then call finish again. Do not repeat actions that already succeeded "
                 "(for example, do not send the same email twice)."})
    return {"verification": result, "status": "retry", "verify_attempts": attempts, "messages": msgs,
            "mode": "agent", "finish": None}


def _blocked(run_id: str) -> list[str]:
    f = evidence.run_dir(run_id) / "steps.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    return [f"{r['rule']}: {r['action']} '{r.get('element') or ''}'" for r in rows if r["event"] == "blocked"]


def report(s: State) -> dict:
    status = s.get("status") or ("failed_step_budget" if s.get("turns", 0) >= config.MAX_AGENT_TURNS else "stopped")
    intent = s["task"].get("intent")
    learned = ""
    if status == "verified" and intent and intent != "unknown":
        if s.get("mode") == "agent" or s.get("escalation") or s.get("healed"):
            steps = procedures.compile_trace(s.get("trace", []), s.get("facts", {}),
                                             tuple((s["task"].get("entities") or {}).keys()))
            v = procedures.save(intent, steps, healed=bool(s.get("healed") or s.get("escalation")))
            learned = f"Procedure '{intent}' saved as version {v} ({len(steps)} steps)."
        else:
            procedures.mark_run(intent)
            learned = f"Reused procedure '{intent}' without changes."
    d = evidence.run_dir(s["run_id"])
    if BROWSER.page is not None:
        try:
            BROWSER.screenshot(d / "screens" / "99_final.png")
        except Exception:
            pass
    screens = sorted(p.name for p in (d / "screens").glob("*.png"))
    ver = s.get("verification") or {}
    md = [f"# Run {s['run_id']}", "", f"**Request:** {s['request']}", f"**Status:** {status}",
          f"**Mode:** {'replay' if s.get('replay_idx') else 'agent'}{' + escalated to agent' if s.get('escalation') else ''}"
          f"{' (self-healed)' if s.get('healed') else ''}",
          f"**Agent summary:** {(s.get('finish') or {}).get('summary', '-')}", "",
          "## Verification (read back from the ERP API)", *[f"- {x}" for x in ver.get("details", [])], "",
          "## Facts used", "```json", json.dumps(s.get("facts", {}), indent=2), "```", "",
          f"## Approvals\n{len(s.get('approvals', []))} granted", "",
          "## Blocked by policy", *([f"- {x}" for x in _blocked(s['run_id'])] or ["- none"]), "",
          f"## Cost\nLLM calls this process: {llm.STATS['calls']} (cache hits: {llm.STATS['cache_hits']}), agent turns: {s.get('turns', 0)}",
          "", f"## Learning\n{learned or 'Nothing saved (run not verified).'}", "",
          "## Evidence", "- steps.jsonl (every action and observation)", *[f"- screens/{x}" for x in screens]]
    (d / "report.md").write_text("\n".join(md))
    evidence.log(s["run_id"], "report", status=status, learned=learned)
    return {"status": status}


# ---------------- wiring ----------------
def after_route(s): return "replay" if s["mode"] == "replay" else "agent"


def after_agent(s):
    if s.get("turns", 0) >= config.MAX_AGENT_TURNS:
        return "report"
    return "gate" if s.get("pending") else "agent"


def after_tools(s): return "verify" if s.get("finish") else "agent"


def after_replay(s): return {"verify": "verify", "approve": "approve", "agent": "agent"}[s["mode"]]


def after_approve(s): return "replay" if s["mode"] == "replay" else "report"


def after_verify(s): return "agent" if s.get("status") == "retry" else "report"


def build(checkpointer):
    g = StateGraph(State)
    for name, fn in [("intake", intake), ("route", route), ("agent", agent), ("gate", gate), ("tools", tools),
                     ("replay", replay), ("approve", approve), ("verify", verify), ("report", report)]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "route")
    g.add_conditional_edges("route", after_route, ["replay", "agent"])
    g.add_conditional_edges("agent", after_agent, ["gate", "agent", "report"])
    g.add_edge("gate", "tools")
    g.add_conditional_edges("tools", after_tools, ["verify", "agent"])
    g.add_conditional_edges("replay", after_replay, ["verify", "approve", "agent"])
    g.add_conditional_edges("approve", after_approve, ["replay", "report"])
    g.add_conditional_edges("verify", after_verify, ["agent", "report"])
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
