"""Offline end-to-end tests for all workflows, with a scripted stand-in for the LLM.

Uses the real ERP, a real headless browser, the real policy gate, verifier and
procedure memory. Only the model's decisions are scripted, so this checks the
runtime, not model quality. No API key or quota needed.

  python -m sandbox.seed && python tests/test_offline_e2e_v2.py
"""
import email
import json
import re
import shutil
import sqlite3
import sys
import threading
import time
from email import policy as email_policy
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ai_operator import config, llm  # noqa: E402

config.LLM_CACHE = False
for f in (config.PROCEDURE_DB, config.STATE_DB):
    f.unlink(missing_ok=True)

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from ai_operator import policy  # noqa: E402
from ai_operator.graph import build  # noqa: E402
from ai_operator.tools.browser import BROWSER  # noqa: E402

RENAMES = {"Vendor": "Supplier", "Invoice number": "Bill reference", "Amount (INR)": "Invoice total",
           "Due date": "Payment due by", "Notes": "Remarks", "Save payable": "Record bill"}
RENAMES.update({v: k for k, v in list(RENAMES.items())})

EXTRACT = {
    "invoice_no": r"(?:Invoice No: |invoice )([A-Z]{2,3}-[\d-]+)",
    "amount": r"Grand Total \(INR\): ([\d,\.]+)",
    "due_date": r"Payment Due: (\S+)",
    "claim_no": r"(CLM-\d+)",
    "decision": r"Decision: (.+)",
    "approved_amount": r"Approved amount: (?:Rs\. )?([\d,]+)",
    "sender_email": r"From: .*<(.+?)>",
    "new_vendor_name": r"Company: (.+)",
    "billing_email": r"Billing email: (\S+)",
    "patient_id": r"Patient ID: (\S+)",
    "department": r"Department: (.+)",
    "discharge_date": r"Date of Discharge: (\S+)",
}


def refs(text):
    return {name: ref for ref, role, name in re.findall(r'\[(e\d+)\] (\w+) "([^"]*)"', text)}


def call(name, **args):
    return {"id": f"c{time.time_ns()}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def line_items(user):
    doc = user.split("Document:\n", 1)[1]
    if "|" in doc:  # contract: "item | rate"
        return [{"item": a.strip(), "rate": b.strip()} for a, b in re.findall(r"^(.+?) \| ([\d\.]+)$", doc, re.M)]
    lines = doc.split("Line total\n", 1)[1].split("Grand Total")[0].strip().split("\n")
    return [{"item": lines[i], "rate": lines[i + 2].replace(",", "")} for i in range(0, len(lines) - 3, 4)]


def intake(user):
    req = user.split("Request: ", 1)[1]
    if "against" in req and (m := re.search(r"([A-Z][\w]+ [A-Z][\w]+) invoice", req)):
        return {"intent": "check_invoice_against_contract", "entities": {"vendor": m.group(1)}}
    if m := re.search(r"(P-\d+)", req):
        return {"intent": "book_discharge_followup", "entities": {"patient_id": m.group(1)}}
    if req.lower().startswith("what"):
        return {"intent": "answer_from_company_documents", "entities": {}}
    if m := re.search(r"invoice from ([A-Z][\w]+ [A-Z][\w]+)", req):
        return {"intent": "record_vendor_invoice", "entities": {"vendor": m.group(1)}}
    if m := re.search(r"(CLM-\d+)", req):
        return {"intent": "update_claim_status", "entities": {"claim_no": m.group(1)}}
    if "payment" in req.lower():
        return {"intent": "answer_vendor_payment_query", "entities": {"vendor": "PharmaLink Distributors"}}
    if "regist" in req.lower():
        return {"intent": "route_vendor_onboarding", "entities": {"new_vendor_name": "SterilePro Instruments"}}
    return {"intent": "unknown", "entities": {}}


def extract(user):
    keys = re.search(r"Use these exact keys: (\[.*?\])", user).group(1)
    specs = re.search(r"Fields: (\[.*?\])\n", user).group(1)
    doc = user.split("Document:\n", 1)[1]
    out = {}
    for k in json.loads(keys.replace("'", '"')):
        if k == "vendor":
            out[k] = doc.split("\n")[0].strip()
        elif k == "followup_days":
            dept = re.search(r"followup_days \(for ([^)]+)\)", specs).group(1)
            m = re.search(rf"{dept}: (\d+) days", doc)
            out[k] = m.group(1) if m else None
        else:
            m = re.search(EXTRACT[k], doc)
            out[k] = m.group(1).strip() if m else None
    return out


def agent_step(intent, n, last, facts):
    r = refs(last)
    if intent == "record_vendor_invoice":
        plan = {
            0: lambda: [call("search_mail", query=f"{facts['vendor']} invoice", attachments_only=True)],
            1: lambda: [call("extract_fields", path=json.loads(last.split("\nNOTE")[0])[0]["attachments"][0],
                             fields=["invoice_no", "amount", "due_date", "vendor"])],
            2: lambda: [call("browser_navigate", url="/payables/new")],
            3: lambda: [call("browser_select", ref=r["Vendor"], option=facts["vendor"]),
                        call("browser_fill", ref=r["Invoice number"], value=facts["invoice_no"]),
                        call("browser_fill", ref=r["Amount (INR)"], value=str(facts["amount"])),
                        call("browser_fill", ref=r["Due date"], value=facts["due_date"]),
                        call("browser_fill", ref=r["Notes"], value="Entered by AI operator from email")],
        }
        if n in plan:
            return plan[n]()
        if "Payable saved" in last:
            return [call("finish", summary=f"Recorded {facts['invoice_no']}", status="done")]
        return [call("browser_click", ref=r.get("Save payable") or r.get("Record bill"))]

    if intent == "update_claim_status":
        plan = {
            0: lambda: [call("search_mail", query=facts["claim_no"])],
            1: lambda: [call("extract_fields", path=json.loads(last)[0]["body_path"],
                             fields=["claim_no", "decision (one of: Approved, Query raised, Rejected)", "approved_amount"])],
            2: lambda: [call("browser_navigate", url="/claims")],
            3: lambda: [call("browser_click", ref=r[facts["claim_no"]])],
            4: lambda: [call("browser_click", ref=r["Edit claim"])],
            5: lambda: [call("browser_select", ref=r["Status"], option=facts["decision"]),
                        call("browser_fill", ref=r["Approved amount (INR)"], value=str(facts["approved_amount"])),
                        call("browser_fill", ref=r["Remarks"], value=f"TPA: {facts['decision']}")],
            6: lambda: [call("browser_click", ref=r["Save claim"])],
        }
        return plan[n]() if n in plan else [call("finish", summary="Claim updated", status="done")]

    if intent == "answer_vendor_payment_query":
        plan = {
            0: lambda: [call("search_mail", query="PharmaLink payment")],
            1: lambda: [call("extract_fields", path=json.loads(last)[0]["body_path"], fields=["invoice_no", "sender_email"])],
            2: lambda: [call("browser_navigate", url=f"/payables?q={facts['invoice_no']}")],
            3: lambda: [call("browser_click", ref=r[facts["invoice_no"]])],
            4: lambda: [call("browser_read_field", label="Status", key="payment_status"),
                        call("browser_read_field", label="Due date", key="due_date")],
            5: lambda: [call("send_mail", to=facts["sender_email"], subject=f"Re: Payment status for invoice {facts['invoice_no']}",
                             body=f"Invoice {facts['invoice_no']} status: {facts['payment_status']}. Due date: {facts['due_date']}.")],
        }
        return plan[n]() if n in plan else [call("finish", summary="Replied", status="done")]

    if intent == "route_vendor_onboarding":
        plan = {
            0: lambda: [call("search_mail", query="vendor registration")],
            1: lambda: [call("extract_fields", path=json.loads(last)[0]["body_path"], fields=["new_vendor_name", "billing_email"])],
            # A careless model tries to add the vendor itself. The gate must block it.
            2: lambda: [call("browser_navigate", url="/vendors/new")],
            3: lambda: [call("browser_fill", ref=r["Vendor name"], value=facts["new_vendor_name"]),
                        call("browser_click", ref=r["Add vendor"])],
            4: lambda: [call("send_mail", to="procurement@sahyadricare.example",
                             subject=f"Vendor registration request: {facts['new_vendor_name']}",
                             body=f"{facts['new_vendor_name']} asked to be registered. Billing: {facts['billing_email']}")],
        }
        return plan[n]() if n in plan else [call("finish", summary="Routed to procurement", status="done")]
    if intent == "check_invoice_against_contract":
        plan = {
            0: lambda: [call("search_mail", query=f"{facts['vendor']} invoice", attachments_only=True)],
            1: lambda: (CURRENT.__setitem__("pdf", json.loads(last.split("\nNOTE")[0])[0]["attachments"][0]),
                        [call("extract_fields", path=CURRENT["pdf"], fields=["invoice_no"])])[1],
            2: lambda: [call("search_documents", query=f"{facts['vendor']} rate contract")],
            3: lambda: [call("compare_line_items", document_path=CURRENT["pdf"], reference_path=json.loads(last)[0]["path"])],
            4: lambda: [call("send_mail", to="ap-manager@sahyadricare.example",
                             subject=f"Rate check {facts['invoice_no']}: {facts['rate_check_result']}",
                             body=f"{facts['vendor']}: {facts['rate_check_details']}")],
        }
        return plan[n]() if n in plan else [call("finish", summary="Rate check sent", status="done")]

    if intent == "book_discharge_followup":
        plan = {
            0: lambda: [call("search_documents", query=f"{facts['patient_id']} discharge summary")],
            1: lambda: [call("extract_fields", path=json.loads(last)[0]["path"],
                             fields=["patient_id", "department", "discharge_date"])],
            2: lambda: [call("search_documents", query=f"follow-up protocol {facts['department']}")],
            3: lambda: [call("extract_fields", path=json.loads(last)[0]["path"],
                             fields=[f"followup_days (for {facts['department']})"])],
            4: lambda: [call("compute_date", base_date=facts["discharge_date"], days=facts["followup_days"], label="followup_date")],
            5: lambda: [call("browser_navigate", url="/appointments/new")],
            6: lambda: [call("browser_fill", ref=r["Patient ID"], value=facts["patient_id"]),
                        call("browser_select", ref=r["Department"], option=facts["department"]),
                        call("browser_fill", ref=r["Appointment date"], value=facts["followup_date"]),
                        call("browser_fill", ref=r["Reason"], value="Post-discharge review")],
            7: lambda: [call("browser_click", ref=r["Book appointment"])],
        }
        return plan[n]() if n in plan else [call("finish", summary="Booked", status="done")]

    if intent == "answer_from_company_documents":
        if n == 0:
            return [call("search_documents", query="payment terms Apex Diagnostics contract")]
        top = json.loads(last)[0]
        return [call("finish", summary=f"15 days from invoice date ({top['path'].split('/')[-1]}, p.{top['page']})", status="done")]
    return [call("finish", summary="no plan", status="blocked")]


def fake_chat(messages, tools=None, json_mode=False, temperature=0):
    llm.STATS["calls"] += 1
    if json_mode:
        user = messages[-1]["content"]
        if "Known procedures" in user:
            out = {**intake(user), "goal": "do it", "clarification": None}
        elif "Use these exact keys" in user:
            out = extract(user)
        elif "priced line item" in messages[0]["content"]:
            out = {"items": line_items(user)}
        else:  # self-heal
            old = re.search(r"named '([^']+)'", user).group(1)
            out = {"ref": refs(user).get(RENAMES.get(old, old))}
        return {"role": "assistant", "content": json.dumps(out)}
    sysmsg = messages[0]["content"]
    facts = json.loads(sysmsg.split("Run memory (facts observed so far): ")[1])
    intent = CURRENT["intent"]
    n = sum(1 for m in messages if m["role"] == "assistant")
    last = next((m["content"] for m in reversed(messages) if m["role"] == "tool"), "")
    return {"role": "assistant", "content": "", "tool_calls": agent_step(intent, n, last, facts)}


CURRENT = {"intent": None}
llm.chat = fake_chat
llm.chat_json = lambda system, user: json.loads(fake_chat(
    [{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)["content"])


def start_erp():
    from sandbox.erp_app import app
    srv = uvicorn.Server(uvicorn.Config(app, port=8800, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(config.ERP_URL)
            return
        except httpx.HTTPError:
            time.sleep(0.1)


def run(graph, request, run_id, intent, approve=True):
    CURRENT["intent"] = intent
    cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 200}
    shutil.rmtree(config.RUNS_DIR / run_id, ignore_errors=True)
    before = llm.STATS["calls"]
    res = graph.invoke({"request": request, "run_id": run_id, "use_procedures": True}, cfg)
    approvals = 0
    while res.get("__interrupt__"):
        approvals += 1
        res = graph.invoke(Command(resume={"approved": approve, "note": "test"}), cfg)
    return res, llm.STATS["calls"] - before, approvals


def api(resource, **q):
    return httpx.get(f"{config.ERP_URL}/api/{resource}", params=q).json()


def outbox():
    msgs = []
    for p in sorted(config.OUTBOX_DIR.glob("*.eml")):
        m = email.message_from_bytes(p.read_bytes(), policy=email_policy.default)
        msgs.append((str(m["to"]), str(m["subject"]), m.get_body(("plain",)).get_content()))
    return msgs


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  OK {msg}")


if __name__ == "__main__":
    start_erp()
    graph = build(SqliteSaver(sqlite3.connect(config.STATE_DB, check_same_thread=False)))
    shutil.rmtree(config.OUTBOX_DIR, ignore_errors=True)

    print("\n== Policy rules (unit) ==")
    check(policy.evaluate("send_mail", {"to": "x@vendor.example", "body": "Patient P-104 was discharged"}, {})["effect"] == "block",
          "patient ID to an external address is blocked")
    check(policy.evaluate("send_mail", {"to": "x@vendor.example", "body": "Invoice paid"}, {})["effect"] == "require_approval",
          "external email needs approval")
    check(policy.evaluate("send_mail", {"to": "procurement@sahyadricare.example", "body": "P-104"}, {}) is None,
          "internal email is allowed")

    from ai_operator.verifier import run_checks
    old = run_checks([{"check": "erp_record", "resource": "payables", "match": {"invoice_no": "MS-2026-0871"},
                       "expect": {"vendor": "MedSupply Co"}}], {}, since=time.time())
    check(not old["passed"] and "before this run" in " ".join(old["details"]),
          "a record that existed before the run does not count as this run's work")

    print("\n== 1. Vendor invoice: learn (agent mode, approval) ==")
    r, c1, a = run(graph, "Record the latest invoice from MedSupply Co in the ERP", "w1", "record_vendor_invoice")
    check(r["status"] == "verified" and a == 1, f"verified with 1 approval, {c1} calls")

    print("\n== 2. Vendor invoice: replay for another vendor ==")
    r, c2, a = run(graph, "Record the latest invoice from Apex Diagnostics in the ERP", "w2", "record_vendor_invoice")
    check(r["status"] == "verified" and c2 < c1, f"verified by replay, {c2} calls vs {c1}")

    print("\n== 3. Vendor invoice: UI drift + flaky submit ==")
    httpx.post(f"{config.ERP_URL}/admin/reset")
    httpx.post(f"{config.ERP_URL}/admin/chaos", params={"flaky_submit": True, "rename_fields": True})
    r, c3, _ = run(graph, "Record the latest invoice from Apex Diagnostics in the ERP", "w3", "record_vendor_invoice")
    check(r["status"] == "verified", f"verified after self-heal and retry, {c3} calls")
    httpx.post(f"{config.ERP_URL}/admin/reset")

    print("\n== 4. TPA claim decision: learn ==")
    r, c4, _ = run(graph, "Update claim CLM-2209 from the TPA's latest email", "w4", "update_claim_status")
    row = api("claims", claim_no="CLM-2209")[0]
    check(r["status"] == "verified" and row["status"] == "Approved" and row["approved_amount"] == 142000,
          f"CLM-2209 Approved, 1,42,000 parsed as 142000 ({c4} calls)")

    print("\n== 5. TPA claim decision: replay on a different claim and outcome ==")
    r, c5, _ = run(graph, "Update claim CLM-2214 from the TPA's latest email", "w5", "update_claim_status")
    row = api("claims", claim_no="CLM-2214")[0]
    check(r["status"] == "verified" and row["status"] == "Query raised" and c5 < c4,
          f"CLM-2214 Query raised via replay ({c5} calls vs {c4})")

    print("\n== 6. Vendor payment query: lookup + external reply with approval ==")
    r, c6, a = run(graph, "Reply to PharmaLink's payment status query", "w6", "answer_vendor_payment_query")
    sent = [m for m in outbox() if "pharmalink" in m[0]]
    check(r["status"] == "verified" and a == 1 and "Payment scheduled" in sent[-1][2],
          f"reply approved, sent, and states the ERP status ({c6} calls)")

    print("\n== 7. Vendor onboarding: forbidden action blocked, request routed ==")
    r, c7, _ = run(graph, "SterilePro sent a vendor registration request, please handle it", "w7", "route_vendor_onboarding")
    names = [v["name"] for v in api("vendors")]
    check("SterilePro Instruments" not in names, "vendor master unchanged (POL-SEC-01 block)")
    check(r["status"] == "verified" and any("procurement@" in m[0] for m in outbox()), "request forwarded to procurement")

    print("\n== 8. Invoice vs rate contract: find the contract, compare, notify AP ==")
    r, c8, _ = run(graph, "Check the latest MedSupply Co invoice against our rate contract", "w8", "check_invoice_against_contract")
    mail = [m for m in outbox() if m[0].startswith("ap-manager")][-1]
    check(r["status"] == "verified" and "MISMATCH" in mail[1] and "21.50" in mail[2] and "19.00" in mail[2],
          f"IV cannula flagged at 21.50 vs contracted 19.00 ({c8} calls)")
    r, c8b, _ = run(graph, "Check the latest Apex Diagnostics invoice against our rate contract", "w8b", "check_invoice_against_contract")
    mail = [m for m in outbox() if m[0].startswith("ap-manager")][-1]
    check(r["status"] == "verified" and "AD-55120: MATCH" in mail[1], f"Apex matches its contract, via replay ({c8b} calls)")

    print("\n== 9. Discharge follow-up: find summary by content, interval from the protocol PDF ==")
    r, c9, _ = run(graph, "Book the post-discharge follow-up for patient P-104", "w9", "book_discharge_followup")
    check(r["status"] == "verified" and api("appointments", patient_id="P-104")[0]["date"] == "2026-10-05",
          f"P-104 Cardiology +7 days = 2026-10-05 ({c9} calls)")
    r, c9b, _ = run(graph, "Book the post-discharge follow-up for patient P-107", "w9b", "book_discharge_followup")
    appt = api("appointments", patient_id="P-107")[0]
    check(r["status"] == "verified" and appt["date"] == "2026-10-14" and appt["department"] == "Orthopedics" and c9b < c9,
          f"P-107 Orthopedics +14 days = 2026-10-14 via replay ({c9b} calls vs {c9})")

    print("\n== 10. Question answered from documents, with source ==")
    r, _, _ = run(graph, "What are the payment terms in our contract with Apex Diagnostics?", "w10", "answer_from_company_documents")
    check(r["status"] == "answered" and "Rate_Contract_Apex" in r["finish"]["summary"], f"answered: {r['finish']['summary']}")

    print("\n== 11. Approver declines: nothing written ==")
    r, _, _ = run(graph, "Record the latest invoice from MedSupply Co in the ERP", "w11", "record_vendor_invoice", approve=False)
    check(r["status"] == "denied" and not api("payables", invoice_no="MS-2026-0934"), "ERP unchanged")

    BROWSER.close()
    print("\nAll offline scenarios passed.")
