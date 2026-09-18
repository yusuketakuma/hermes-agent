---
name: skill-fin-invoice-generation
description: "Create, validate, preview, approve, and issue invoices through Finance Core without using Hermes Memory as the source of truth."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, invoice, billing, japanese-invoice, 経理, 請求書]
    related_skills: [skill-fin-customer-management, skill-fin-payment-status-check, skill-fin-revenue-tracking, skill-fin-spreadsheet-integration]
---

# Finance invoice generation

Use the independent Finance Core MCP server for every financial fact. Do not
write customer names, addresses, email addresses, bank details, or invoice
documents to Hermes Memory, GBrain, ordinary chat summaries, or logs.

## Workflow

1. Resolve an existing `issuer_id` and `customer_id` with
   `finance.customer.search` or `finance.customer.get`. If a master is
   missing, stop and request a controlled master-data operation; do not invent
   a customer or issuer. Customer address, email, and bank fields stay inside
   Finance Core.
2. For recurring, hourly, event, expense, milestone, discount, adjustment, or
   manual billing, read the active contract and `finance.billing_rule.*` first.
   Use `finance.invoice.draft_from_rule` so quantity, price, tax category,
   due date, and the calculation expression come from the versioned rule.
   Reuse a stable `billing_key` on retries.
3. Call `finance.template.list_versions` and use the approved
   `invoice-standard-jp@1.0.0` version unless the operator selected another
   approved version.
4. Call `finance.invoice.draft_create` with explicit line `tax_category` and
   `tax_rate`. Choose one of `STANDARD_10`, `REDUCED_8`, `EXEMPT`, or
   `OUT_OF_SCOPE`; never infer tax category from the line description.
   For recurring or batch work, call `finance.invoice.bulk_draft_create` with a
   stable, unique `billing_key` for every request. Re-running the same batch
   returns existing IDs instead of creating duplicate drafts. Use the optional
   `document_type` for `invoice`, `quote`, `delivery_note`, `purchase_order`,
   or `receipt` when the approved template supports it.
   If withholding applies, provide an explicitly approved
   `withholding_tax_rate` and `withholding_tax_base`; never infer it from the
   customer or description.
5. Call `finance.invoice.validate`. If it is invalid, show the errors and stop.
6. Call `finance.invoice.preview_pdf` and report the preview path and totals.
   The preview must remain DRAFT and must not receive a formal invoice number.
7. Wait for a separate human approval. Then call
   `finance.invoice.approve` from the authenticated Hermes session. Do not
   substitute a typed display name for the session identity.
8. Only after approval call `finance.invoice.issue` from that same authenticated
   session. Issuing creates the
   immutable canonical JSON/PDF pair, assigns the next invoice number, stores
   the master snapshot, and records the audit event.

For timesheet-driven billing, record each approved operational result with
`finance.work.record`, review `finance.work.list`, and use
`finance.work.draft_from_entries` with a stable `billing_key`. Work entries
are marked billed only after the draft is created.

If an accounting period is closed, stop and report the period-lock error. Do
not reopen it implicitly. Approval policies may require a separate creator,
approver, issuer, or sender and may impose an amount limit.

Invoice issue and external delivery are separate. Never send a PDF merely
because it was issued; obtain a separate delivery approval and use the
configured delivery workflow. After approval, call
`finance.delivery.prepare` with `email`, `web`, `postal`, or `discord`; use a
separately authorized adapter for the actual external operation. For an email
delivery, call `finance.delivery.dispatch_email` only from the authenticated
send session. For a Discord delivery, pass the resolved accounting channel ID
as `recipient` and call `finance.delivery.dispatch_discord` only after delivery
approval. Both adapters claim a queued, issued-PDF Outbox item and never send
a draft. For other adapters, record the provider result with
`finance.delivery.update_status`. Record recipient downloads with
`finance.delivery.record_download`.

For an issued-document error, use `finance.invoice.correct` with a reason;
never edit or replace the original PDF. Use `finance.invoice.convert` to
derive a delivery note, invoice, or receipt from a prior document while
retaining the lineage. Both operations require an authenticated actor and a
new draft/approval cycle.
