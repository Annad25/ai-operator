---
intent: update_claim_status
title: Update an insurance claim from a TPA decision email
inputs: [claim_no]
success_checks:
  - check: erp_record
    resource: claims
    match: {claim_no: "{claim_no}"}
    expect: {status: "{decision}", approved_amount: "{approved_amount}"}
---
# SOP-BILL-12: Recording TPA claim decisions

Owner: Billing and Insurance desk, Sahyadri Care Hospital.

1. TPA decisions arrive by email to accounts@sahyadricare.example. Find the latest email about the claim number.
2. Extract from the email body: claim_no, decision (one of: Approved, Query raised, Rejected), approved_amount (number in INR, 0 if none).
   Indian number format such as 1,42,000 means 142000.
3. Open the claim in the ERP under Insurance claims (/claims), then Edit claim.
4. Set Status to the decision and Approved amount to the approved amount.
   Remarks: copy the TPA's deduction or query line, prefixed with "TPA:".
5. Save, and confirm the claim page shows the new status.
