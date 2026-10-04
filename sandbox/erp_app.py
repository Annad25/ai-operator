"""Sahyadri Care Hospital back-office ERP (mock).

A plain server-rendered web app the agent operates through a real browser.
The JSON API under /api is read-only and is used ONLY by the verifier,
so verification never relies on what the agent says it did.

Chaos toggles (POST /admin/chaos) simulate real-world breakage:
  flaky_submit  - every other form submit returns HTTP 503 (form is kept)
  rename_fields - form labels and button text change (UI drift)

Run: uvicorn sandbox.erp_app:app --port 8800
"""
import html
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

DB = Path(__file__).parent / "data" / "erp.db"
DEPARTMENTS = ["Cardiology", "Orthopedics", "General Medicine", "Neurology"]

app = FastAPI(title="Sahyadri Care ERP (sandbox)")
CHAOS = {"flaky_submit": False, "rename_fields": False}
_submit_counter = {"n": 0}

LABELS = {
    False: {"vendor": "Vendor", "invoice_no": "Invoice number", "amount": "Amount (INR)",
            "due_date": "Due date", "notes": "Notes", "save": "Save payable"},
    True: {"vendor": "Supplier", "invoice_no": "Bill reference", "amount": "Invoice total",
           "due_date": "Payment due by", "notes": "Remarks", "save": "Record bill"},
}

CSS = """
body{margin:0;font:15px/1.5 "Segoe UI",Roboto,Arial,sans-serif;color:#1d2a33;background:#f3f6f7}
header{background:#12415a;color:#fff;padding:10px 24px;display:flex;gap:24px;align-items:center}
header strong{font-size:16px;margin-right:16px}
header a{color:#cfe6ef;text-decoration:none}
header a:hover,header a:focus{color:#fff;text-decoration:underline}
main{max-width:860px;margin:24px auto;padding:0 20px}
h1{font-size:22px;margin:0 0 16px}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #dde5e8}
th{background:#e7eef1;font-weight:600}
form{background:#fff;padding:20px;border:1px solid #d3dde1;max-width:480px}
label{display:block;font-weight:600;margin-top:12px}
input,select,textarea{width:100%;padding:7px;border:1px solid #9fb2bb;font:inherit;box-sizing:border-box}
input:focus,select:focus,textarea:focus,button:focus{outline:3px solid #f2b134}
button{margin-top:18px;background:#1b6b4f;color:#fff;border:0;padding:9px 18px;font:inherit;cursor:pointer}
.banner{padding:10px 14px;margin-bottom:14px;border-left:5px solid}
.ok{background:#e4f4ec;border-color:#1b6b4f}.err{background:#fbe9e7;border-color:#b3261e}
.actions a{margin-right:14px}
"""


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)} - Sahyadri ERP</title>
<style>{CSS}</style></head><body>
<header><strong>Sahyadri Care ERP</strong><a href="/">Home</a><a href="/payables">Payables</a>
<a href="/appointments">Appointments</a><a href="/patients">Patients</a><a href="/claims">Insurance claims</a>
<a href="/vendors">Vendors</a></header>
<main><h1>{html.escape(title)}</h1>{body}</main></body></html>""",
        status_code=status,
    )


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


@app.get("/", response_class=HTMLResponse)
def home():
    return page("Home", """<p class="actions"><a href="/payables/new">Record a vendor invoice</a>
<a href="/appointments/new">Book an appointment</a></p>
<p>Accounts payable and outpatient scheduling for Sahyadri Care Hospital, Pune.</p>""")


# ---------------- Payables ----------------
@app.get("/payables", response_class=HTMLResponse)
def payables(q: str = ""):
    con = db()
    rows = con.execute(
        "SELECT * FROM payables WHERE vendor LIKE ? OR invoice_no LIKE ? ORDER BY id DESC",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    trs = "".join(
        f"<tr><td><a href='/payables/{r['id']}'>{esc(r['invoice_no'])}</a></td><td>{esc(r['vendor'])}</td>"
        f"<td>{r['amount']:,.2f}</td><td>{esc(r['due_date'])}</td><td>{esc(r['status'])}</td></tr>"
        for r in rows
    ) or "<tr><td colspan=5>No payables match this search.</td></tr>"
    return page("Payables", f"""<form method="get" action="/payables" style="max-width:none;margin-bottom:14px">
<label for="q">Search by vendor or invoice number</label><input id="q" name="q" value="{esc(q)}">
<button type="submit">Search</button></form>
<p class="actions"><a href="/payables/new">Record a vendor invoice</a></p>
<table><tr><th>Invoice</th><th>Vendor</th><th>Amount (INR)</th><th>Due</th><th>Status</th></tr>{trs}</table>""")


def payable_form(values: dict | None = None, error: str = "", status: int = 200):
    values = values or {}
    L = LABELS[CHAOS["rename_fields"]]
    vendors = [r["name"] for r in db().execute("SELECT name FROM vendors ORDER BY name")]
    opts = "<option value=''>Select…</option>" + "".join(
        f"<option {'selected' if v == values.get('vendor') else ''}>{esc(v)}</option>" for v in vendors
    )
    banner = f"<div class='banner err' role='alert'>{esc(error)}</div>" if error else ""
    return page("Record a vendor invoice", f"""{banner}<form method="post" action="/payables">
<label for="vendor">{L['vendor']}</label><select id="vendor" name="vendor" required>{opts}</select>
<label for="invoice_no">{L['invoice_no']}</label><input id="invoice_no" name="invoice_no" required value="{esc(values.get('invoice_no'))}">
<label for="amount">{L['amount']}</label><input id="amount" name="amount" type="number" step="0.01" required value="{esc(values.get('amount'))}">
<label for="due_date">{L['due_date']}</label><input id="due_date" name="due_date" type="date" required value="{esc(values.get('due_date'))}">
<label for="notes">{L['notes']}</label><textarea id="notes" name="notes">{esc(values.get('notes'))}</textarea>
<button type="submit">{L['save']}</button></form>""", status)


@app.get("/payables/new", response_class=HTMLResponse)
def payable_new():
    return payable_form()


@app.post("/payables")
def payable_create(vendor: str = Form(""), invoice_no: str = Form(""), amount: str = Form(""),
                   due_date: str = Form(""), notes: str = Form("")):
    values = dict(vendor=vendor, invoice_no=invoice_no, amount=amount, due_date=due_date, notes=notes)
    if CHAOS["flaky_submit"]:
        _submit_counter["n"] += 1
        if _submit_counter["n"] % 2 == 1:
            return payable_form(values, "Service temporarily unavailable (503). Your entries were kept. Submit again.", 503)
    if not all([vendor, invoice_no, amount, due_date]):
        return payable_form(values, "Vendor, invoice number, amount and due date are required.", 422)
    try:
        amt = float(amount)
    except ValueError:
        return payable_form(values, "Amount must be a number.", 422)
    con = db()
    if con.execute("SELECT 1 FROM payables WHERE invoice_no=?", (invoice_no,)).fetchone():
        return payable_form(values, f"Invoice {invoice_no} is already recorded. Duplicate entries are blocked.", 409)
    cur = con.execute("INSERT INTO payables(vendor, invoice_no, amount, due_date, notes) VALUES (?,?,?,?,?)",
                      (vendor, invoice_no, amt, due_date, notes))
    con.commit()
    return RedirectResponse(f"/payables/{cur.lastrowid}?saved=1", status_code=303)


@app.get("/payables/{pid}", response_class=HTMLResponse)
def payable_detail(pid: int, saved: int = 0):
    r = db().execute("SELECT * FROM payables WHERE id=?", (pid,)).fetchone()
    if not r:
        return page("Payable not found", "<p>No payable with this ID.</p>", 404)
    banner = "<div class='banner ok' role='status'>Payable saved.</div>" if saved else ""
    return page(f"Payable {r['invoice_no']}", f"""{banner}<table>
<tr><th>Vendor</th><td>{esc(r['vendor'])}</td></tr><tr><th>Invoice number</th><td>{esc(r['invoice_no'])}</td></tr>
<tr><th>Amount (INR)</th><td>{r['amount']:,.2f}</td></tr><tr><th>Due date</th><td>{esc(r['due_date'])}</td></tr>
<tr><th>Status</th><td>{esc(r['status'])}</td></tr><tr><th>Notes</th><td>{esc(r['notes'])}</td></tr></table>""")


# ---------------- Appointments ----------------
@app.get("/patients", response_class=HTMLResponse)
def patients():
    rows = db().execute("SELECT * FROM patients ORDER BY id").fetchall()
    trs = "".join(f"<tr><td>{esc(r['id'])}</td><td>{esc(r['name'])}</td><td>{esc(r['department'])}</td></tr>" for r in rows)
    return page("Patients", f"<table><tr><th>ID</th><th>Name</th><th>Department</th></tr>{trs}</table>")


@app.get("/appointments", response_class=HTMLResponse)
def appointments():
    rows = db().execute("SELECT * FROM appointments ORDER BY id DESC").fetchall()
    trs = "".join(f"<tr><td>{esc(r['patient_id'])}</td><td>{esc(r['department'])}</td><td>{esc(r['date'])}</td>"
                  f"<td>{esc(r['reason'])}</td></tr>" for r in rows) or "<tr><td colspan=4>No appointments booked yet.</td></tr>"
    return page("Appointments", f"""<p class="actions"><a href="/appointments/new">Book an appointment</a></p>
<table><tr><th>Patient</th><th>Department</th><th>Date</th><th>Reason</th></tr>{trs}</table>""")


def appt_form(values=None, error="", status=200):
    values = values or {}
    opts = "<option value=''>Select…</option>" + "".join(
        f"<option {'selected' if d == values.get('department') else ''}>{d}</option>" for d in DEPARTMENTS)
    banner = f"<div class='banner err' role='alert'>{esc(error)}</div>" if error else ""
    return page("Book an appointment", f"""{banner}<form method="post" action="/appointments">
<label for="patient_id">Patient ID</label><input id="patient_id" name="patient_id" required value="{esc(values.get('patient_id'))}">
<label for="department">Department</label><select id="department" name="department" required>{opts}</select>
<label for="date">Appointment date</label><input id="date" name="date" type="date" required value="{esc(values.get('date'))}">
<label for="reason">Reason</label><input id="reason" name="reason" value="{esc(values.get('reason'))}">
<button type="submit">Book appointment</button></form>""", status)


@app.get("/appointments/new", response_class=HTMLResponse)
def appt_new():
    return appt_form()


@app.post("/appointments")
def appt_create(patient_id: str = Form(""), department: str = Form(""), date: str = Form(""), reason: str = Form("")):
    values = dict(patient_id=patient_id, department=department, date=date, reason=reason)
    if CHAOS["flaky_submit"]:
        _submit_counter["n"] += 1
        if _submit_counter["n"] % 2 == 1:
            return appt_form(values, "Service temporarily unavailable (503). Your entries were kept. Submit again.", 503)
    con = db()
    if not con.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        return appt_form(values, f"No patient with ID {patient_id}.", 422)
    con.execute("INSERT INTO appointments(patient_id, department, date, reason) VALUES (?,?,?,?)",
                (patient_id, department, date, reason))
    con.commit()
    return page("Appointment booked", f"<div class='banner ok' role='status'>Appointment booked for {esc(patient_id)} on {esc(date)}.</div>"
                "<p class='actions'><a href='/appointments'>View appointments</a></p>")


# ---------------- Insurance claims ----------------
CLAIM_STATUSES = ["Submitted", "Query raised", "Approved", "Rejected"]


@app.get("/claims", response_class=HTMLResponse)
def claims():
    rows = db().execute("SELECT * FROM claims ORDER BY id DESC").fetchall()
    trs = "".join(
        f"<tr><td><a href='/claims/{r['id']}'>{esc(r['claim_no'])}</a></td><td>{esc(r['patient_id'])}</td>"
        f"<td>{esc(r['tpa'])}</td><td>{r['claimed_amount']:,.2f}</td><td>{esc(r['status'])}</td></tr>" for r in rows)
    return page("Insurance claims", "<table><tr><th>Claim</th><th>Patient</th><th>TPA</th><th>Claimed (INR)</th>"
                f"<th>Status</th></tr>{trs}</table>")


@app.get("/claims/{cid}", response_class=HTMLResponse)
def claim_detail(cid: int, saved: int = 0):
    r = db().execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    if not r:
        return page("Claim not found", "<p>No claim with this ID.</p>", 404)
    banner = "<div class='banner ok' role='status'>Claim updated.</div>" if saved else ""
    approved = f"{r['approved_amount']:,.2f}" if r["approved_amount"] is not None else "Not decided"
    return page(f"Claim {r['claim_no']}", f"""{banner}<table>
<tr><th>Claim number</th><td>{esc(r['claim_no'])}</td></tr><tr><th>Patient ID</th><td>{esc(r['patient_id'])}</td></tr>
<tr><th>TPA</th><td>{esc(r['tpa'])}</td></tr><tr><th>Claimed amount (INR)</th><td>{r['claimed_amount']:,.2f}</td></tr>
<tr><th>Approved amount (INR)</th><td>{approved}</td></tr><tr><th>Status</th><td>{esc(r['status'])}</td></tr>
<tr><th>Remarks</th><td>{esc(r['remarks'])}</td></tr></table>
<p class="actions"><a href="/claims/{cid}/edit">Edit claim</a></p>""")


@app.get("/claims/{cid}/edit", response_class=HTMLResponse)
def claim_edit(cid: int):
    r = db().execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    if not r:
        return page("Claim not found", "<p>No claim with this ID.</p>", 404)
    opts = "".join(f"<option {'selected' if st == r['status'] else ''}>{st}</option>" for st in CLAIM_STATUSES)
    return page(f"Edit claim {r['claim_no']}", f"""<form method="post" action="/claims/{cid}">
<label for="status">Status</label><select id="status" name="status">{opts}</select>
<label for="approved_amount">Approved amount (INR)</label><input id="approved_amount" name="approved_amount" type="number" step="0.01" value="{esc(r['approved_amount'])}">
<label for="remarks">Remarks</label><textarea id="remarks" name="remarks">{esc(r['remarks'])}</textarea>
<button type="submit">Save claim</button></form>""")


@app.post("/claims/{cid}")
def claim_update(cid: int, status: str = Form(""), approved_amount: str = Form(""), remarks: str = Form("")):
    if status not in CLAIM_STATUSES:
        return page("Edit claim", f"<div class='banner err' role='alert'>Unknown status {esc(status)}.</div>", 422)
    amt = float(approved_amount) if approved_amount.strip() else None
    con = db()
    con.execute("UPDATE claims SET status=?, approved_amount=?, remarks=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, amt, remarks, cid))
    con.commit()
    return RedirectResponse(f"/claims/{cid}?saved=1", status_code=303)


# ---------------- Vendor master ----------------
@app.get("/vendors", response_class=HTMLResponse)
def vendors():
    rows = db().execute("SELECT * FROM vendors ORDER BY name").fetchall()
    trs = "".join(f"<tr><td>{esc(r['id'])}</td><td>{esc(r['name'])}</td><td>{esc(r['email'])}</td></tr>" for r in rows)
    return page("Vendors", f"""<p class="actions"><a href="/vendors/new">Add a vendor</a></p>
<table><tr><th>ID</th><th>Name</th><th>Email</th></tr>{trs}</table>""")


@app.get("/vendors/new", response_class=HTMLResponse)
def vendor_new():
    return page("Add a vendor", """<form method="post" action="/vendors">
<label for="name">Vendor name</label><input id="name" name="name" required>
<label for="email">Billing email</label><input id="email" name="email" required>
<button type="submit">Add vendor</button></form>""")


@app.post("/vendors")
def vendor_create(name: str = Form(""), email: str = Form("")):
    con = db()
    n = con.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] + 1
    con.execute("INSERT INTO vendors VALUES (?,?,?)", (f"V-{n:03d}", name, email))
    con.commit()
    return RedirectResponse("/vendors", status_code=303)


# ---------------- Read-only API (verifier) + admin ----------------
@app.get("/api/{resource}")
def api(resource: str, request: Request):
    if resource not in {"payables", "appointments", "vendors", "patients", "claims"}:
        return JSONResponse({"error": "unknown resource"}, 404)
    filters = dict(request.query_params)
    cols = {r[1] for r in db().execute(f"PRAGMA table_info({resource})")}
    where = [f"{k}=?" for k in filters if k in cols]
    sql = f"SELECT * FROM {resource}" + (" WHERE " + " AND ".join(where) if where else "")
    rows = db().execute(sql, [filters[k] for k in filters if k in cols]).fetchall()
    return [dict(r) for r in rows]


@app.post("/admin/chaos")
def set_chaos(flaky_submit: bool = False, rename_fields: bool = False):
    CHAOS.update(flaky_submit=flaky_submit, rename_fields=rename_fields)
    _submit_counter["n"] = 0
    return CHAOS


@app.post("/admin/reset")
def reset():
    from sandbox.seed import init_erp_db
    init_erp_db(DB)
    CHAOS.update(flaky_submit=False, rename_fields=False)
    return {"reset": True}
