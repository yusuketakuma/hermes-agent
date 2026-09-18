---
name: skill-fin-discord-finance-workflow
description: "Canonical Discord natural-language entry point for Finance Core invoice, customer, calendar billing, payment, approval, and delivery operations."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, accounting, invoice, billing, payment, revenue, discord, 経理, 請求書, 入金]
    related_skills: [skill-fin-invoice-generation, skill-fin-customer-management, skill-fin-payment-status-check, skill-fin-revenue-tracking]
---

# Discord Finance Core workflow

Finance Core is the only source of truth. The removed local `accounting` tool is
not available. Do not write a second local invoice record, or store customer
PII, bank data, or invoice documents in Hermes Memory, GBrain, chat summaries,
or ordinary logs.

## First-time authorization setup

When the authenticated operator explicitly asks to make the current session a
Finance Core administrator, call `finance.approval.bootstrap_self`. Never pass
a guessed Discord user ID. The bootstrap is self-only when no approval policy
exists; adding other approvers requires an active administrator through
`finance.approval.policy_set`.

## Natural-language routing

Translate the user's request into the existing `finance.*` MCP tools. Resolve
IDs first and show a redacted summary before any approval-sensitive action.

- Customer or issuer lookup: `finance.customer.search`, `finance.customer.get`,
  and `finance.template.list_versions`.
- Direct invoice draft: active contract/rule, then
  `finance.invoice.draft_from_rule` or `finance.invoice.draft_create`, followed
  by `finance.invoice.validate` and `finance.invoice.preview_pdf`.
- Invoice status, overdue, revenue, or payment: use the corresponding
  `finance.invoice.*`, `finance.receivables.*`, `finance.revenue.*`,
  `finance.dashboard.*`, and `finance.payment.*` tools.
- Customer creation or update: use the exact Finance Core schema. A customer
  profile uses `billing_name`, `billing_address`, `postal_code`, `department`,
  `contact_name`, `email`, `phone`, `fax`, payment terms, delivery method, and
  other documented profile fields. `legal_name` and `address` are accepted as
  aliases for the billing name and address; do not invent other nested fields.

## Visit-pharmacy calendar billing

For requests such as “訪問薬剤管理カレンダーの8月実績から請求書を作成”:

1. List calendar connections and select the exact name `訪問薬剤管理`.
2. Synchronize the selected connection with `finance.calendar.sync`.
3. List and map schedules to an existing customer, contract, and billing rule.
4. Complete each visit with operator-confirmed actual start and end times.
   Never use the planned interval as billable time.
5. Create work entries, approve the work entries, and run
   `finance.billing_run.preview`.
6. Show unmapped, incomplete, cancelled, already-billed, and unapproved items.
   Finalize only the explicitly selected eligible schedule IDs.
7. Create the invoice draft from the finalized billing run, validate it, and
   render the DRAFT PDF.

A calendar event alone never creates, issues, or sends an invoice.

## Approval and delivery boundary

The following are separate user turns and separate authenticated operations:

1. Draft and DRAFT PDF preview.
2. Explicit invoice approval.
3. Formal issue and immutable PDF/JSON creation.
4. Separate delivery approval and delivery dispatch.

Never infer approval from “作成して”, “確認して”, or a preview request. For
external delivery use `finance.delivery.prepare`; supported methods are
`email`, `web`, `postal`, and `discord`. For Discord delivery, resolve the
accounting channel with the Discord channel tools and pass its channel ID as
the delivery recipient. Dispatch with `finance.delivery.dispatch_discord` only
after the separate delivery approval. A failed or uncertain delivery must be
reported and recovered through the outbox; never silently retry.

All approval and write actor fields must be supplied by the authenticated
Hermes session. Do not substitute a display name or a model-generated user ID.
