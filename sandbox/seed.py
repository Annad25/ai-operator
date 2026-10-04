"""Creates the sandbox company: Sahyadri Care Hospital (fictional).

Everything here is mock data. Run: python -m sandbox.seed
Produces sandbox/data/{erp.db, mailbox/*.eml, shared_drive/...}
"""
import shutil
import sqlite3
from email.message import EmailMessage
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

DATA = Path(__file__).parent / "data"

VENDORS = [
    ("V-001", "MedSupply Co", "billing@medsupply.example"),
    ("V-002", "Apex Diagnostics", "accounts@apexdiag.example"),
    ("V-003", "CleanCare Linen", "invoices@cleancare.example"),
    ("V-004", "PharmaLink Distributors", "ar@pharmalink.example"),
]

PATIENTS = [
    ("P-104", "Rahul Kulkarni", "Cardiology"),
    ("P-107", "Meera Joshi", "Orthopedics"),
    ("P-112", "Imran Shaikh", "General Medicine"),
]

DEPARTMENTS = ["Cardiology", "Orthopedics", "General Medicine", "Neurology"]

# (vendor_name, invoice_no, invoice_date, amount, due_date, items, email_date)
INVOICES = [
    ("MedSupply Co", "MS-2026-0871", "2026-08-30", 41200.00, "2026-09-29",
     [("Nitrile gloves (box of 100)", 400, 58.0), ("Surgical masks (box of 50)", 300, 60.0)],
     "Sat, 30 Aug 2026 10:12:00 +0530"),
    ("MedSupply Co", "MS-2026-0934", "2026-09-27", 68450.00, "2026-10-27",
     [("IV cannula 20G", 1500, 21.5), ("Syringes 5ml (box of 100)", 220, 164.0)],
     "Sun, 27 Sep 2026 09:41:00 +0530"),
    ("Apex Diagnostics", "AD-55120", "2026-09-25", 12300.00, "2026-10-10",
     [("Lipid profile reagent kit", 3, 4100.0)],
     "Thu, 25 Sep 2026 16:05:00 +0530"),
    # Two CleanCare invoices on the same day: "latest" is ambiguous on purpose.
    ("CleanCare Linen", "CC-7781", "2026-09-26", 18900.00, "2026-10-26",
     [("Bed linen laundering (kg)", 900, 21.0)],
     "Fri, 26 Sep 2026 11:00:00 +0530"),
    ("CleanCare Linen", "CC-7782", "2026-09-26", 7400.00, "2026-10-26",
     [("OT gown sterilisation (pcs)", 370, 20.0)],
     "Fri, 26 Sep 2026 11:00:00 +0530"),
]

# (file_name, patient_id, name, department, admit, discharge, attending)
# File names do not contain patient IDs, so the agent has to search document contents.
DISCHARGES = [
    ("DS_2026_0412.pdf", "P-104", "Rahul Kulkarni", "Cardiology", "2026-09-22", "2026-09-28", "Dr. S. Deshpande"),
    ("DS_2026_0419.pdf", "P-107", "Meera Joshi", "Orthopedics", "2026-09-24", "2026-09-30", "Dr. A. Patil"),
    ("DS_2026_0398.pdf", "P-112", "Imran Shaikh", "General Medicine", "2026-09-15", "2026-09-20", "Dr. R. Kale"),
]

# Company documents in the shared drive: (folder, file, title, lines). Blank string = paragraph break.
DOCUMENTS = [
    ("contracts", "Rate_Contract_MedSupply_Co_2026.pdf", "Rate Contract RC-2026-014", [
        "Between: Sahyadri Care Hospital, Pune (Buyer) and MedSupply Co (Supplier)",
        "Validity: 2026-04-01 to 2027-03-31", "Payment terms: 30 days from invoice date.", "",
        "Contracted rates (INR per unit, excluding GST):",
        "IV cannula 20G | 19.00", "Syringes 5ml (box of 100) | 164.00",
        "Nitrile gloves (box of 100) | 58.00", "Surgical masks (box of 50) | 60.00", "",
        "Invoices above contracted rates will be held until a credit note is received.",
    ]),
    ("contracts", "Rate_Contract_Apex_Diagnostics_2026.pdf", "Rate Contract RC-2026-021", [
        "Between: Sahyadri Care Hospital, Pune (Buyer) and Apex Diagnostics (Supplier)",
        "Validity: 2026-07-01 to 2027-06-30", "Payment terms: 15 days from invoice date.", "",
        "Contracted rates (INR per unit, excluding GST):",
        "Lipid profile reagent kit | 4100.00", "HbA1c reagent kit | 3650.00",
    ]),
    ("contracts", "Service_Agreement_CleanCare_Linen.pdf", "Service Agreement SA-2025-007", [
        "Between: Sahyadri Care Hospital, Pune and CleanCare Linen (Service provider)",
        "Validity: 2025-10-01 to 2026-12-31", "Payment terms: 30 days from invoice date.", "",
        "Agreed rates (INR, excluding GST):",
        "Bed linen laundering (kg) | 21.00", "OT gown sterilisation (pcs) | 19.00",
    ]),
    ("manuals", "Accounts_Payable_Manual_v2.pdf", "Accounts Payable Manual v2", [
        "1. Approval matrix for payables",
        "Up to INR 50,000: AP officer. INR 50,000 to 5,00,000: Finance Controller. Above INR 5,00,000: CFO.", "",
        "2. Rate checks",
        "Every supplier invoice is checked line by line against the supplier's rate contract.",
        "If any line is above the contracted rate, the invoice is put on hold and the AP manager",
        "(ap-manager@sahyadricare.example) is informed with the item, invoiced rate and contracted rate.", "",
        "3. Payment runs", "Payment runs happen every Wednesday. Payment status values: Pending payment, Payment scheduled, Paid.",
    ]),
    ("manuals", "Procurement_Manual_v3.pdf", "Procurement Manual v3", [
        "4. Vendor onboarding checklist",
        "Before a vendor is added to the vendor master, Procurement collects:",
        "GST registration certificate; PAN card; cancelled cheque for bank details;",
        "Drug license (for pharmaceuticals and medical consumables); ISO 13485 certificate (for surgical instruments);",
        "Signed NDA and code of conduct.", "",
        "Requests are sent to procurement@sahyadricare.example. Only Procurement may edit the vendor master.",
    ]),
    ("clinical", "Clinical_Followup_Protocol_2026.pdf", "Post-discharge Follow-up Protocol", [
        "Follow-up review interval after discharge, by department:",
        "Cardiology: 7 days", "Orthopedics: 14 days", "General Medicine: 10 days", "Neurology: 7 days", "",
        "Book the review in the same department. Appointment reason: Post-discharge review.",
    ]),
]


def _invoice_pdf(path: Path, vendor, inv_no, inv_date, amount, due, items):
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, vendor)
    y -= 22
    c.setFont("Helvetica", 11)
    c.drawString(50, y, "TAX INVOICE")
    y -= 30
    for line in [
        f"Invoice No: {inv_no}",
        f"Invoice Date: {inv_date}",
        "Bill To: Sahyadri Care Hospital, Pune",
        f"Payment Due: {due}",
    ]:
        c.drawString(50, y, line)
        y -= 18
    y -= 12
    c.drawString(50, y, "Description")
    c.drawString(330, y, "Qty")
    c.drawString(400, y, "Rate")
    c.drawString(470, y, "Line total")
    y -= 16
    for desc, qty, rate in items:
        c.drawString(50, y, desc)
        c.drawString(330, y, str(qty))
        c.drawString(400, y, f"{rate:,.2f}")
        c.drawString(470, y, f"{qty * rate:,.2f}")
        y -= 16
    y -= 14
    c.setFont("Helvetica-Bold", 12)
    c.drawString(330, y, f"Grand Total (INR): {amount:,.2f}")
    c.save()


def _discharge_pdf(path: Path, pid, name, dept, admit, discharge, doctor):
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    c.setFont("Helvetica-Bold", 15)
    c.drawString(50, y, "Sahyadri Care Hospital - Discharge Summary")
    c.setFont("Helvetica", 11)
    y -= 30
    for line in [
        f"Patient ID: {pid}",
        f"Patient Name: {name}",
        f"Department: {dept}",
        f"Attending Physician: {doctor}",
        f"Date of Admission: {admit}",
        f"Date of Discharge: {discharge}",
        "Condition at discharge: Stable",
        "Advice: Follow-up review as per hospital discharge protocol.",
    ]:
        c.drawString(50, y, line)
        y -= 18
    c.save()


def _text_pdf(path: Path, title: str, lines: list[str]):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(50, 800, "Sahyadri Care Hospital")
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, 778, title)
    c.setFont("Helvetica", 10.5)
    y = 750
    for line in lines:
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 10.5)
            y = 800
        c.drawString(50, y, line)
        y -= 10 if line == "" else 16
    c.save()


def init_erp_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        DROP TABLE IF EXISTS vendors; DROP TABLE IF EXISTS patients;
        DROP TABLE IF EXISTS payables; DROP TABLE IF EXISTS appointments;
        CREATE TABLE vendors(id TEXT PRIMARY KEY, name TEXT, email TEXT);
        CREATE TABLE patients(id TEXT PRIMARY KEY, name TEXT, department TEXT);
        CREATE TABLE payables(id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT,
            invoice_no TEXT UNIQUE, amount REAL, due_date TEXT, notes TEXT,
            status TEXT DEFAULT 'Pending payment', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        DROP TABLE IF EXISTS claims;
        CREATE TABLE claims(id INTEGER PRIMARY KEY AUTOINCREMENT, claim_no TEXT UNIQUE, patient_id TEXT,
            tpa TEXT, claimed_amount REAL, approved_amount REAL, status TEXT, remarks TEXT, updated_at TEXT);
        CREATE TABLE appointments(id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT,
            department TEXT, date TEXT, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """
    )
    con.executemany("INSERT INTO vendors VALUES (?,?,?)", VENDORS)
    con.executemany("INSERT INTO patients VALUES (?,?,?)", PATIENTS)
    # The August MedSupply invoice is already entered, so "latest" matters.
    con.execute(
        "INSERT INTO payables(vendor, invoice_no, amount, due_date, notes, status) VALUES (?,?,?,?,?,?)",
        ("MedSupply Co", "MS-2026-0871", 41200.00, "2026-09-29", "Consumables Aug", "Paid"),
    )
    con.execute(
        "INSERT INTO payables(vendor, invoice_no, amount, due_date, notes, status) VALUES (?,?,?,?,?,?)",
        ("PharmaLink Distributors", "PL-3301", 96500.00, "2026-10-08", "Antibiotics Sep", "Payment scheduled"),
    )
    con.executemany(
        "INSERT INTO claims(claim_no, patient_id, tpa, claimed_amount, approved_amount, status, remarks) VALUES (?,?,?,?,?,?,?)",
        [("CLM-2209", "P-104", "Suraksha TPA", 158000.00, None, "Submitted", ""),
         ("CLM-2214", "P-107", "Suraksha TPA", 212500.00, None, "Submitted", ""),
         ("CLM-2190", "P-112", "Arogya Health TPA", 64000.00, 64000.00, "Approved", "Settled")],
    )
    con.commit()
    con.close()


def main():
    if DATA.exists():
        shutil.rmtree(DATA)
    mailbox = DATA / "mailbox"
    attach = DATA / "mailbox" / "attachments"
    drive = DATA / "shared_drive" / "discharge_summaries"
    for d in (mailbox, attach, drive):
        d.mkdir(parents=True, exist_ok=True)

    init_erp_db(DATA / "erp.db")

    vendor_email = {v[1]: v[2] for v in VENDORS}
    for i, (vendor, inv_no, inv_date, amount, due, items, email_date) in enumerate(INVOICES, 1):
        pdf = attach / f"{inv_no}.pdf"
        _invoice_pdf(pdf, vendor, inv_no, inv_date, amount, due, items)
        msg = EmailMessage()
        msg["From"] = f"{vendor} <{vendor_email[vendor]}>"
        msg["To"] = "accounts@sahyadricare.example"
        msg["Subject"] = f"Invoice {inv_no} from {vendor}"
        msg["Date"] = email_date
        msg.set_content(f"Dear Accounts team,\n\nPlease find attached invoice {inv_no}.\n\nRegards,\n{vendor}")
        msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
        (mailbox / f"{i:03d}_{inv_no}.eml").write_bytes(bytes(msg))

    # Noise email so search has to discriminate
    msg = EmailMessage()
    msg["From"] = "MedSupply Co <billing@medsupply.example>"
    msg["To"] = "accounts@sahyadricare.example"
    msg["Subject"] = "Diwali holiday schedule"
    msg["Date"] = "Mon, 29 Sep 2026 12:00:00 +0530"
    msg.set_content("Our office will be closed 20-22 Oct.")
    (mailbox / "099_notice.eml").write_bytes(bytes(msg))

    # Emails without attachments: TPA decisions, a vendor payment query, a vendor onboarding request
    plain = [
        ("Suraksha TPA <claims@surakshatpa.example>", "Claim CLM-2209 - decision", "Wed, 01 Oct 2026 15:20:00 +0530",
         "Dear Sahyadri Care billing team,\n\nClaim CLM-2209 for patient P-104 has been reviewed.\n"
         "Decision: Approved\nApproved amount: Rs. 1,42,000\nDeduction: Rs. 16,000 (non-payable consumables)\n\nSuraksha TPA"),
        ("Suraksha TPA <claims@surakshatpa.example>", "Claim CLM-2214 - query", "Thu, 02 Oct 2026 11:05:00 +0530",
         "Dear Sahyadri Care billing team,\n\nClaim CLM-2214 for patient P-107 is on hold.\n"
         "Decision: Query raised\nApproved amount: 0\nQuery: please share the implant invoice and sticker.\n\nSuraksha TPA"),
        ("PharmaLink Distributors <ar@pharmalink.example>", "Payment status for invoice PL-3301",
         "Thu, 02 Oct 2026 10:15:00 +0530",
         "Hello,\n\nCould you confirm the payment status and expected date for our invoice PL-3301?\n\n"
         "Thanks,\nRitu Sharma\nAccounts Receivable, PharmaLink Distributors"),
        ("SterilePro Instruments <onboarding@sterilepro.example>", "Vendor registration - SterilePro Instruments",
         "Fri, 03 Oct 2026 09:30:00 +0530",
         "Hello,\n\nWe would like to be registered as a vendor for surgical instruments.\n"
         "Company: SterilePro Instruments\nGSTIN: 27ABCDE1234F1Z5 (sandbox)\nContact: Kunal Mehta, +91 90000 00000\n"
         "Billing email: billing@sterilepro.example\n\nRegards,\nKunal"),
    ]
    for j, (frm, subj, dt, body) in enumerate(plain, 200):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"], msg["Date"] = frm, "accounts@sahyadricare.example", subj, dt
        msg.set_content(body)
        (mailbox / f"{j}_plain.eml").write_bytes(bytes(msg))

    (DATA / "outbox").mkdir(parents=True, exist_ok=True)

    for fname, pid, name, dept, admit, dis, doc in DISCHARGES:
        _discharge_pdf(drive / fname, pid, name, dept, admit, dis, doc)
    for folder, fname, title, lines in DOCUMENTS:
        (DATA / "shared_drive" / folder).mkdir(parents=True, exist_ok=True)
        _text_pdf(DATA / "shared_drive" / folder / fname, title, lines)

    print(f"Sandbox seeded at {DATA}")


if __name__ == "__main__":
    main()
