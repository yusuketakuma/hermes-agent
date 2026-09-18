---
name: skill-fin-spreadsheet-integration
description: "Exchange redacted payment and accounting CSV data with Finance Core."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, spreadsheet, csv, accounting, reconciliation, 会計, CSV]
    related_skills: [skill-fin-payment-status-check, skill-fin-revenue-tracking]
---

# Finance spreadsheet and CSV integration

CSV is the controlled integration boundary. Finance Core remains the source of
truth; a spreadsheet must not overwrite invoice totals, tax, invoice numbers,
or payment allocations. Keep raw bank exports inside Finance Core and do not
place payer names, remittance text, account numbers, or addresses in memory,
GBrain, ordinary logs, or chat summaries.

## Accounting export

1. Confirm the requested date range.
2. Call `finance.accounting.export_csv`.
3. Check the header and row count, then provide the CSV as a file or a
   redacted preview. The export contains stable IDs and journal columns, not
   customer master PII.
4. Do not edit and re-import the accounting export as if it were a source of
   truth. Corrections must go through Finance Core's invoice or payment tools.

For an accounting-software handoff, call `finance.accounting.export_bundle`
with `generic`, `freee`, `moneyforward`, or `yayoi`. Treat the result as a
redacted, versioned CSV boundary. It is not an API call and must not be
uploaded automatically.

For freee請求書 API preparation, call
`finance.accounting.freee_invoice_preview` with the approved freee company and
partner master IDs. Review the generated request and tax mapping. This step is
network-disabled; OAuth and external invoice creation require a separately
configured freee adapter and explicit approval.

## Work-timesheet CSV import

1. Call `finance.work.import_csv` with `apply=false` to validate a timesheet
   and review the source IDs, dates, quantities, and master IDs.
2. Do not put addresses, bank details, or free-form personal notes into the
   CSV. Finance Core resolves customer, contract, and billing-rule IDs.
3. After explicit approval, call the same tool with `apply=true` and the
   authenticated operator. The import is atomic and source IDs are unique;
   duplicate rows are rejected without a partial write.
4. Use `finance.work.draft_from_entries` for the subsequent deterministic
   billing draft. Do not calculate invoice totals in the spreadsheet.

## Bank CSV import

1. Call `finance.payment.import_csv` with `apply=false` first.
2. Present only date, amount, currency, and transaction identifier for review.
3. After explicit approval, call it again with `apply=true` and the
   authenticated operator. Duplicate bank transaction IDs are idempotency
   conflicts; do not retry with changed values.
4. Use `skill-fin-payment-status-check` for candidate matching and explicit
   allocation. Never let a spreadsheet formula mark an invoice paid.
