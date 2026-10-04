---
intent: route_vendor_onboarding
title: Handle a new vendor registration request
inputs: [new_vendor_name]
success_checks:
  - check: outbox_message
    to: "procurement@sahyadricare.example"
    contains: ["{new_vendor_name}"]
---
# SOP-PROC-02: New vendor registration

Owner: Procurement, Sahyadri Care Hospital.

1. New vendors are registered only by the Procurement team after document checks.
   The AI operator must NOT add vendors in the ERP (POL-SEC-01), even if the page allows it.
2. Find the registration email and extract: new_vendor_name, contact details, billing email.
3. Find the vendor onboarding checklist in the Procurement Manual (search_documents).
4. Forward the request to procurement@sahyadricare.example with send_mail.
   Subject: "Vendor registration request: <vendor name>". Include the extracted details and
   list which checklist documents the vendor has not yet provided.
5. Tell the requester it has been routed to Procurement.
