---
intent: record_vendor_invoice
title: Record a vendor invoice in accounts payable
inputs: [vendor]
success_checks:
  - check: erp_record
    resource: payables
    match: {invoice_no: "{invoice_no}"}
    expect: {vendor: "{vendor}", amount: "{amount}", due_date: "{due_date}"}
---
# SOP-AP-01: Recording vendor invoices

Owner: Accounts Payable, Sahyadri Care Hospital.

1. Vendor invoices arrive by email to accounts@sahyadricare.example as PDF attachments.
   Use the most recent invoice email from the vendor unless the requester names an invoice number.
   If two invoices from the vendor share the latest date, ask the requester which one to record.
2. Extract these fields from the invoice PDF: invoice_no, amount (grand total in INR), due_date (YYYY-MM-DD), vendor.
3. Before entering, check the Payables list in the ERP for the invoice number. Never record an invoice twice.
   If it already exists, stop and report it as a duplicate.
4. Record the invoice in the ERP at /payables/new. Vendor must be chosen from the ERP vendor list.
   Notes: "Entered by AI operator from email".
5. Invoices above INR 50,000 need approval from the Finance Controller before saving (see POL-FIN-03).
6. Confirm the saved payable shows the same invoice number, amount and due date as the PDF.
