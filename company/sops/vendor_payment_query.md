---
intent: answer_vendor_payment_query
title: Answer a vendor's question about payment status
inputs: [vendor]
success_checks:
  - check: erp_record
    resource: payables
    match: {invoice_no: "{invoice_no}"}
    expect: {status: "{payment_status}"}
    fresh: false   # a lookup: the record is supposed to exist already; this checks the status read was right
  - check: outbox_message
    to: "{sender_email}"
    contains: ["{invoice_no}", "{payment_status}"]
---
# SOP-AP-04: Vendor payment status queries

Owner: Accounts Payable, Sahyadri Care Hospital.

1. Find the vendor's latest email asking about payment.
2. Extract from the email body: invoice_no and sender_email (the vendor address the email came from).
3. Look up the invoice in the ERP Payables list and open it.
   Record the Status field with read_field (key payment_status) and the Due date (key due_date).
4. Reply to the sender with send_mail. Subject: "Re: <original subject>".
   State the invoice number, the status exactly as shown in the ERP, and the due date. Keep it short and polite.
   Do not promise dates that are not in the ERP.
5. Emails to external addresses need approval from the Accounts Manager (POL-COM-04).
