---
name: skill-fin-payment-status-check
description: "Import bank CSVs, suggest invoice matches, and reconcile receipts through Finance Core with explicit approval."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, payment, reconciliation, receivables, 入金確認, 消込]
    related_skills: [skill-fin-invoice-generation]
---

# Finance payment status and reconciliation

Use Finance Core as the only source of truth for receipts and allocations. Do
not put payer names, remittance text, bank identifiers, account details, or
bank CSV contents into Hermes Memory, GBrain, ordinary logs, or a chat summary.

## Bank CSV workflow

1. Confirm that the operator supplied a bank export and call
   `finance.payment.import_csv` with `apply=false` first.
2. Show only the redacted preview fields: date, amount, currency, and bank
   transaction identifier. Do not echo payer or remittance columns.
3. Stop on any validation error, duplicate transaction, or unexpected
   currency. Do not repair financial values by guessing.
4. Obtain explicit human approval for the exact preview. Only then call
   `finance.payment.import_csv` with `apply=true` and the authenticated
   operator identifier.
5. If the apply result reports an existing transaction, treat it as an
   idempotency conflict and do not retry with altered identifiers.

## Matching and allocation

1. Call `finance.payment.unallocated_list` and
   `finance.payment.match_suggest` for an imported payment.
2. An invoice number plus exact amount is a high-confidence candidate. A
   payer alias, amount, or due-date candidate is only a suggestion; present
   all candidates when more than one remains. Use `finance.payment.alias_add`
   only for a verified remittance-name mapping; the raw payer name stays in
   Finance Core.
3. Before allocation, show the invoice number, allocation amount, and the
   resulting outstanding/overpaid amount. Never allocate an uncertain match
   automatically.
4. After explicit approval, call `finance.payment.allocate` once with the
   complete allocation set and an authenticated approver. Allocation is
   atomic: if any row is invalid, nothing is applied.
5. Call `finance.payment.check` after allocation. Report `UNPAID`,
   `PARTIALLY_PAID`, `PAID`, or `OVERPAID`; calculate overdue from due date and
   outstanding balance rather than inventing a stored `OVERDUE` state.

## Exceptions

- Partial receipts are valid and remain `PARTIALLY_PAID` until fully covered.
- One receipt may be allocated to multiple invoices, and one invoice may have
  multiple receipts.
- A short payment, bank fee, offset, refund, or overpayment requires an
  explicit adjustment workflow; never silently mark an invoice as paid.
- Unmatched receipts remain unallocated for human review.
- Invoice issue and external reminder/email delivery are separate approvals.
- Email or Discord delivery is sent only through the corresponding
  `finance.delivery.dispatch_email` or `finance.delivery.dispatch_discord`
  adapter after the issued invoice is queued and the authenticated send actor
  is present.
- Allocation for a closed accounting period is blocked; request a controlled
  period reopen with a reason if a correction is necessary.
