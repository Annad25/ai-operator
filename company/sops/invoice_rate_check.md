---
intent: check_invoice_against_contract
title: Check a vendor invoice against the vendor's rate contract
inputs: [vendor]
success_checks:
  - check: outbox_message
    to: "ap-manager@sahyadricare.example"
    contains: ["{invoice_no}", "{rate_check_result}"]
---
# SOP-AP-02: Invoice rate check

Owner: Accounts Payable, Sahyadri Care Hospital. Policy source: Accounts Payable Manual, section 2.

1. Find the vendor's latest invoice email and its PDF attachment. Extract invoice_no from the PDF.
2. Find the vendor's rate contract or service agreement in the shared drive with search_documents.
3. Run compare_line_items with the invoice PDF as the document and the contract as the reference.
4. Email the result to ap-manager@sahyadricare.example.
   Subject: "Rate check <invoice_no>: <MATCH or MISMATCH>".
   Body: the vendor, the contract file name, and each line above the contracted rate with both rates.
5. Do not record the invoice in the ERP as part of this task.
