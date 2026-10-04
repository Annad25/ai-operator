---
intent: book_discharge_followup
title: Book a post-discharge follow-up appointment
inputs: [patient_id]
success_checks:
  - check: erp_record
    resource: appointments
    match: {patient_id: "{patient_id}"}
    expect: {department: "{department}", date: "{followup_date}"}
---
# SOP-OPD-07: Post-discharge follow-up

Owner: Outpatient Department, Sahyadri Care Hospital.

1. Discharge summaries are in the shared drive. File names do not contain patient IDs,
   so find the patient's summary with search_documents (for example "<patient id> discharge summary").
2. Extract from the summary: patient_id, department, discharge_date (YYYY-MM-DD).
3. The follow-up interval depends on the department. Find it in the Post-discharge Follow-up Protocol
   (search_documents), then extract followup_days for that department from the protocol.
4. Compute the date with compute_date (base discharge_date, days followup_days, label followup_date).
5. Book it in the ERP at /appointments/new in the same department. Reason: "Post-discharge review".
6. Confirm the appointment appears in the Appointments list.
