"""Independent verification.

The agent acts through the browser. The verifier reads the system of record
through a separate read-only API and compares it with the success checks the
SOP defines, filled with facts that came from tool observations.
"""
import re

import httpx

from . import config


def _fill(template, facts):
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        return facts.get(template[1:-1])
    return template


def _equal(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.01
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def _check_outbox(c, facts, since=None):
    import email
    from email import policy as email_policy
    to = _fill(c["to"], facts)
    needles = [_fill(x, facts) for x in c.get("contains", [])]
    if to is None or any(n is None for n in needles):
        return False, f"FAIL: facts needed for the outbox check are missing (to={to}, contains={needles})"
    for p in sorted(config.OUTBOX_DIR.glob("*.eml"), reverse=True):
        if since and p.stem.isdigit() and int(p.stem) / 1000 < since:
            continue  # sent before this run
        msg = email.message_from_bytes(p.read_bytes(), policy=email_policy.default)
        if to.lower() not in str(msg["to"]).lower():
            continue
        text = f"{msg['subject']}\n{msg.get_body(('plain',)).get_content()}".lower()
        # whole-word match, so "MATCH" is not satisfied by "MISMATCH"
        missing = [n for n in needles if not re.search(rf"(?<![\w-]){re.escape(str(n).lower())}(?![\w-])", text)]
        if not missing:
            return True, f"PASS: email to {to} sent ({p.name}) and mentions {needles}"
        return False, f"FAIL: email to {to} ({p.name}) does not mention {missing}"
    return False, f"FAIL: no email to {to} in the outbox"


def _row_time(row: dict) -> float | None:
    """ERP timestamps are SQLite CURRENT_TIMESTAMP (UTC, 'YYYY-MM-DD HH:MM:SS')."""
    from datetime import datetime, timezone
    ts = row.get("updated_at") or row.get("created_at")
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def run_checks(checks: list[dict], facts: dict, since: float | None = None) -> dict:
    """since: run start time. A record or email that already existed before the run does not count,
    otherwise a run that did nothing could 'pass' on someone else's earlier work."""
    if not checks:
        return {"passed": False, "unverifiable": True,
                "details": ["No success checks are defined for this task, so completion cannot be verified."]}
    details, passed = [], True
    for c in checks:
        if c["check"] == "outbox_message":
            ok, msg = _check_outbox(c, facts, since)
            passed &= ok
            details.append(msg)
            continue
        if c["check"] != "erp_record":
            details.append(f"Unknown check type {c['check']}")
            passed = False
            continue
        match = {k: _fill(v, facts) for k, v in c["match"].items()}
        expect = {k: _fill(v, facts) for k, v in c.get("expect", {}).items()}
        missing = [k for k, v in {**match, **expect}.items() if v is None]
        if missing:
            details.append(f"FAIL: facts needed for verification are missing: {missing}")
            passed = False
            continue
        rows = httpx.get(f"{config.ERP_URL}/api/{c['resource']}", params=match, timeout=10).json()
        if not rows:
            details.append(f"FAIL: no {c['resource']} record matching {match}")
            passed = False
            continue
        row = rows[-1]
        t = _row_time(row)
        if c.get("fresh", True) and since and t is not None and t < since:
            details.append(f"FAIL: the matching {c['resource']} record was created or last changed before this run "
                           f"({row.get('updated_at') or row.get('created_at')} UTC), so this run did not do it. "
                           f"If it is a duplicate, report that instead.")
            passed = False
            continue
        for k, v in expect.items():
            ok = _equal(row.get(k), v)
            passed &= ok
            details.append(f"{'PASS' if ok else 'FAIL'}: {c['resource']}.{k} = {row.get(k)!r} (expected {v!r})")
        details.append(f"Record: {row}")
    return {"passed": passed, "unverifiable": False, "details": details}
