"""Tool registry.

Every tool declares a JSON schema (sent to the LLM), a risk level, and a handler.
Risk levels: read (no side effects), input (local, reversible, e.g. typing into a
form), write (changes company systems; goes through the policy gate).

Handlers return a ToolResult. `facts` are merged into run state, so values the
agent relies on (amount, due date) come from tool observations, not from the
model's own text.
"""
import email
import email.message
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from email import policy as email_policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

from .. import config, llm
from ..evidence import run_dir
from .browser import BROWSER


@dataclass
class ToolResult:
    ok: bool
    output: str                      # what the LLM sees
    data: dict = field(default_factory=dict)    # structured output (used for procedure templating)
    facts: dict = field(default_factory=dict)   # merged into run memory
    target: dict | None = None       # browser element acted on: {role, name}
    needs_judgment: str = ""         # tool detected a decision point (stops blind replay)


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    risk: str
    handler: Callable[[dict, dict], ToolResult]
    optional: tuple = ()

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params,
                           "required": [p for p in self.params if p not in self.optional]}}}


# ---------------- browser ----------------
def _browser_result(snap: dict, target: dict | None = None) -> ToolResult:
    status = snap.get("status") or 200
    ok = status < 400
    out = BROWSER.render(snap)
    if not ok:
        out = f"ACTION REACHED AN ERROR PAGE (HTTP {status}).\n" + out
    return ToolResult(ok=ok, output=out, data={"url": snap["url"], "status": status}, target=target)


def _target(ref):
    e = BROWSER.element(ref)
    return {"role": e["role"], "name": e["name"]} if e else None


def t_navigate(a, ctx):
    return _browser_result(BROWSER.navigate(a["url"]))


def t_snapshot(a, ctx):
    return _browser_result(BROWSER.snapshot())


def t_click(a, ctx):
    tgt = _target(a["ref"])
    return _browser_result(BROWSER.click(a["ref"]), tgt)


def t_fill(a, ctx):
    tgt = _target(a["ref"])
    return _browser_result(BROWSER.fill(a["ref"], a["value"]), tgt)


def t_select(a, ctx):
    tgt = _target(a["ref"])
    return _browser_result(BROWSER.select(a["ref"], a["option"]), tgt)


def t_read_field(a, ctx):
    val = BROWSER.read_field(a["label"])
    if val is None:
        return ToolResult(False, f"No field labelled '{a['label']}' on this page.")
    val = _normalize(a["key"], val)
    return ToolResult(True, f"{a['key']} = {val!r}", data={a["key"]: val}, facts={a["key"]: val})


# ---------------- mailbox ----------------
def t_search_mail(a, ctx):
    q = [t for t in re.findall(r"[a-z0-9]+", a["query"].lower())]
    files_dir = run_dir(ctx["run_id"]) / "files"
    hits = []
    for p in sorted(config.MAILBOX_DIR.glob("*.eml")):
        msg = email.message_from_bytes(p.read_bytes(), policy=email_policy.default)
        body = msg.get_body(("plain",)).get_content()
        if not all(t in f"{msg['from']} {msg['subject']} {body}".lower() for t in q):
            continue
        atts = []
        for part in msg.iter_attachments():
            out = files_dir / part.get_filename()
            out.write_bytes(part.get_content())
            atts.append(str(out.relative_to(config.ROOT)))
        body_path = files_dir / f"{p.stem}.txt"
        body_path.write_text(f"From: {msg['from']}\nSubject: {msg['subject']}\nDate: {msg['date']}\n\n{body}")
        hits.append({"id": p.stem, "from": str(msg["from"]), "subject": str(msg["subject"]),
                     "date": parsedate_to_datetime(msg["date"]).isoformat(), "attachments": atts,
                     "body_path": str(body_path.relative_to(config.ROOT)), "preview": body[:300]})
    hits.sort(key=lambda h: h["date"], reverse=True)
    if a.get("attachments_only"):
        hits = [h for h in hits if h["attachments"]]
    judgment = ""
    if len(hits) > 1 and hits[0]["date"] == hits[1]["date"]:
        judgment = f"{hits[0]['subject']} and {hits[1]['subject']} arrived at the same time"
    note = f"\nNOTE: the two most recent results have the same timestamp ({judgment})." if judgment else ""
    if not hits:
        return ToolResult(False, "No emails match. Try fewer or different words.", data={"results": []})
    return ToolResult(True, json.dumps(hits[:5], indent=1) + note, data={"results": hits[:5]}, needs_judgment=judgment)


def t_send_mail(a, ctx):
    msg = email.message.EmailMessage()
    msg["From"] = "AI Operator <ai-operator@sahyadricare.example>"
    msg["To"], msg["Subject"] = a["to"], a["subject"]
    msg.set_content(a["body"])
    config.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUTBOX_DIR / f"{int(time.time() * 1000)}.eml"
    path.write_bytes(bytes(msg))
    return ToolResult(True, f"Sent to {a['to']} (saved {path.name})", data={"sent_to": a["to"]})


# ---------------- files ----------------
def _safe_path(p: str) -> Path:
    path = (config.ROOT / p).resolve()
    allowed = [config.DRIVE_DIR.resolve(), config.RUNS_DIR.resolve()]
    if not any(str(path).startswith(str(r)) for r in allowed):
        raise PermissionError(f"Access outside the shared drive / run workspace is blocked: {p}")
    return path


def t_search_files(a, ctx):
    q = [t for t in re.findall(r"[a-z0-9]+", a["query"].lower())]
    files = [str(p.relative_to(config.ROOT)) for p in config.DRIVE_DIR.rglob("*")
             if p.is_file() and all(t in p.name.lower() for t in q)]
    return ToolResult(True, json.dumps(files), data={"results": files})


def _doc_chunks():
    """Page-level chunks (6-line windows) of every document in the shared drive."""
    chunks = []
    for f in sorted(config.DRIVE_DIR.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".pdf", ".txt", ".md"):
            continue
        pages = [pg.extract_text() or "" for pg in PdfReader(f).pages] if f.suffix.lower() == ".pdf" else [f.read_text()]
        for pno, text in enumerate(pages, 1):
            lines = [ln for ln in text.splitlines() if ln.strip()]
            for i in range(0, max(len(lines), 1), 4):
                window = lines[max(0, i - 2): i + 6]
                chunks.append({"path": str(f.relative_to(config.ROOT)), "page": pno,
                               "text": " ".join(window), "title": " ".join(lines[:2])})
    return chunks


def t_search_documents(a, ctx):
    """Full-text search (BM25) over the shared drive. Local, no model calls."""
    from rank_bm25 import BM25Okapi
    chunks = _doc_chunks()
    tok = lambda t: re.findall(r"[a-z0-9]+", t.lower())  # noqa: E731
    bm25 = BM25Okapi([tok(c["title"] + " " + c["path"] + " " + c["text"]) for c in chunks])
    scores = bm25.get_scores(tok(a["query"]))
    best, seen = [], set()
    for score, c in sorted(zip(scores, chunks), key=lambda x: -x[0]):
        if score <= 0 or (c["path"], c["page"]) in seen:
            continue
        seen.add((c["path"], c["page"]))
        best.append({"path": c["path"], "page": c["page"], "score": round(float(score), 2), "snippet": c["text"][:280]})
        if len(best) == 5:
            break
    if not best:
        return ToolResult(False, "No documents match. Try other words.", data={"results": []})
    return ToolResult(True, json.dumps(best, indent=1), data={"results": best})


def _line_items(path: str) -> list[dict]:
    text = _read_text(_safe_path(path))
    out = llm.chat_json(
        "Extract every priced line item from the document. Reply with JSON only: "
        '{"items": [{"item": "<description>", "rate": <unit rate as a number>}]}. '
        "Use the unit rate, not the line total. Numbers without currency or commas.",
        f"Document:\n{text[:6000]}")
    items = []
    for it in out.get("items", []):
        try:
            items.append({"item": str(it["item"]).strip(), "rate": float(str(it["rate"]).replace(",", ""))})
        except (KeyError, ValueError, TypeError):
            continue
    return items


def t_compare_line_items(a, ctx):
    """Extract line items from a document and a reference (e.g. invoice vs rate contract) with the model,
    then compare them in code. The model reads; arithmetic and matching are deterministic."""
    from difflib import SequenceMatcher
    doc, ref = _line_items(a["document_path"]), _line_items(a["reference_path"])
    if not doc or not ref:
        return ToolResult(False, f"Could not read line items (document: {len(doc)}, reference: {len(ref)}).")
    rows, problems = [], []
    for d in doc:
        match = max(ref, key=lambda r: SequenceMatcher(None, d["item"].lower(), r["item"].lower()).ratio())
        sim = SequenceMatcher(None, d["item"].lower(), match["item"].lower()).ratio()
        if sim < 0.6:
            rows.append(f"{d['item']}: {d['rate']:.2f}, NOT IN REFERENCE")
            problems.append(f"{d['item']} is not in the reference document")
        elif d["rate"] > match["rate"] + 0.005:
            rows.append(f"{d['item']}: {d['rate']:.2f} vs {match['rate']:.2f} ABOVE")
            problems.append(f"{d['item']} invoiced at {d['rate']:.2f}, contracted {match['rate']:.2f}")
        else:
            rows.append(f"{d['item']}: {d['rate']:.2f} vs {match['rate']:.2f} OK")
    result = "MISMATCH" if problems else "MATCH"
    facts = {"rate_check_result": result, "rate_check_details": "; ".join(problems) or "all lines within contract"}
    return ToolResult(True, f"{result}\n" + "\n".join(rows), data={"result": result, "rows": rows}, facts=facts)


def _read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "\n".join(pg.extract_text() or "" for pg in PdfReader(path).pages)
    return path.read_text(errors="ignore")


def t_read_document(a, ctx):
    text = _read_text(_safe_path(a["path"]))
    return ToolResult(True, text[:4000])


def _normalize(k, v):
    if v is None:
        return v
    if k.endswith("_days"):
        m = re.search(r"\d+", str(v))
        return int(m.group()) if m else v
    if k in ("amount", "total") or k.endswith("_amount"):
        try:
            return float(str(v).replace(",", "").replace("INR", "").replace("₹", "").strip())
        except ValueError:
            return v
    return v


def t_extract_fields(a, ctx):
    """LLM-based extraction to a fixed field list. The values become run facts."""
    text = _read_text(_safe_path(a["path"]))
    specs = a["fields"]
    fields = [re.split(r"\s*\(", f)[0].strip() for f in specs]   # "decision (one of: A, B)" -> key "decision"
    out = llm.chat_json(
        "Extract the requested fields from the document. Reply with one JSON object only. "
        "Dates as YYYY-MM-DD. Amounts as plain numbers without currency or commas. Use null if a field is missing.",
        f"Fields: {specs}\nUse these exact keys: {fields}\n\nDocument:\n{text[:6000]}")
    facts = {k: _normalize(k, out.get(k)) for k in fields}
    missing = [k for k, v in facts.items() if v in (None, "")]
    return ToolResult(not missing, json.dumps(facts) + (f"\nMissing: {missing}" if missing else ""),
                      data=facts, facts={k: v for k, v in facts.items() if v not in (None, "")})


# ---------------- utilities ----------------
def t_compute_date(a, ctx):
    d = date.fromisoformat(a["base_date"]) + timedelta(days=int(a["days"]))
    return ToolResult(True, f"{a['label']} = {d.isoformat()}", data={a["label"]: d.isoformat()},
                      facts={a["label"]: d.isoformat()})


def t_remember(a, ctx):
    return ToolResult(True, f"remembered {a['key']}", facts={a["key"]: a["value"]})


# ask_human and finish are handled by the runtime (gate / verify nodes); handlers are placeholders.
def _runtime_only(a, ctx):
    return ToolResult(True, "")


S = {"type": "string"}
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("browser_navigate", "Open a page in a company web app. Relative paths are resolved against the ERP base URL.",
         {"url": S}, "read", t_navigate),
    Tool("browser_snapshot", "Re-read the current page.", {}, "read", t_snapshot),
    Tool("browser_click", "Click an element by ref from the latest snapshot.", {"ref": S}, "write", t_click),
    Tool("browser_fill", "Type a value into a textbox by ref (replaces the current value). Dates as YYYY-MM-DD.",
         {"ref": S, "value": S}, "input", t_fill),
    Tool("browser_select", "Choose an option in a dropdown by ref and the option's visible text.",
         {"ref": S, "option": S}, "input", t_select),
    Tool("browser_read_field", "Read the value shown next to a label on the current page (detail tables or form fields) "
         "and store it in run memory under key.", {"label": S, "key": S}, "read", t_read_field),
    Tool("search_mail", "Search the accounts mailbox (all words must match). Newest first. Each result has body_path "
         "(email text as a file, usable with extract_fields) and saved attachment paths.",
         {"query": S, "attachments_only": {"type": "boolean"}}, "read", t_search_mail, optional=("attachments_only",)),
    Tool("send_mail", "Send an email from the AI operator mailbox.", {"to": S, "subject": S, "body": S}, "write", t_send_mail),
    Tool("search_files", "Find files in the shared drive whose names contain all query words.", {"query": S}, "read", t_search_files),
    Tool("search_documents", "Search the CONTENTS of company documents in the shared drive (contracts, manuals, "
         "protocols, discharge summaries). Returns path, page and a snippet for the best matches.",
         {"query": S}, "read", t_search_documents),
    Tool("compare_line_items", "Compare the priced line items of a document (e.g. an invoice) against a reference "
         "document (e.g. a rate contract). Stores rate_check_result (MATCH or MISMATCH) and rate_check_details.",
         {"document_path": S, "reference_path": S}, "read", t_compare_line_items),
    Tool("read_document", "Read the text of a PDF or text file.", {"path": S}, "read", t_read_document),
    Tool("extract_fields", "Extract named fields from a document or email body into run memory. A field may carry a hint, "
         "e.g. 'decision (one of: Approved, Rejected)'. Prefer this over copying values yourself.",
         {"path": S, "fields": {"type": "array", "items": S}}, "read", t_extract_fields),
    Tool("compute_date", "Add days to a YYYY-MM-DD date and store the result in run memory under label.",
         {"base_date": S, "days": {"type": "integer"}, "label": S}, "read", t_compute_date),
    Tool("remember", "Store a fact in run memory.", {"key": S, "value": S}, "read", t_remember),
    Tool("ask_human", "Ask the requester a question when you cannot safely decide. Use only for real ambiguity.",
         {"question": S}, "read", _runtime_only),
    Tool("finish", "Call when the task is complete (or cannot be completed). The result will be independently verified.",
         {"summary": S, "status": {"type": "string", "enum": ["done", "blocked"]}}, "read", _runtime_only),
]}


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS.values()]


def execute(name: str, args: dict, ctx: dict) -> ToolResult:
    tool = TOOLS.get(name)
    if not tool:
        return ToolResult(False, f"Unknown tool {name}")
    if "_invalid_json" in args:
        return ToolResult(False, "Your tool arguments were not valid JSON. Call the tool again.")
    try:
        return tool.handler(args, ctx)
    except Exception as e:  # tool errors are observations, not crashes
        return ToolResult(False, f"ERROR {type(e).__name__}: {e}")
