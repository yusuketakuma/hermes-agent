---
name: skill-fin-revenue-tracking
description: "Summarize revenue, receivables, and invoice reminders through Finance Core."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, revenue, receivables, reporting, reminders, 売上, 未収, 督促]
    related_skills: [skill-fin-invoice-generation, skill-fin-payment-status-check]
---

# Finance revenue and receivables tracking

Use Finance Core for every amount and date. Do not infer revenue from a chat
message, an invoice's delivery status, or a bank notification. Do not copy
customer names, addresses, contact details, or raw payment data into memory,
GBrain, ordinary logs, or report text.

## Reporting workflow

1. Confirm an inclusive `from_date` and `to_date` in `YYYY-MM-DD` format.
2. Call `finance.revenue.summary` for the total invoice, received,
   outstanding, and recognized-revenue amounts.
3. Call `finance.revenue.by_customer`, `finance.revenue.by_month`, or
   `finance.revenue.by_project` only when the operator requests a breakdown.
   Present `customer_id`, not customer PII.
4. Use `finance.sales.search` for recognized-revenue entries and
   `finance.sales.record` only after confirming the source, amount, and
   recognition date. The source key is idempotent.
5. Explain the three dates separately: service period for revenue recognition,
   issue date for receivables, and value date for cash receipt.
6. Use `finance.receivables.overdue_list` for invoices already past due and
   `finance.receivables.reminder_candidates` for due-soon or overdue candidates.
   `reminder_days` is a look-ahead window and does not approve or send a notice.
7. Use `finance.dashboard.summary` for the combined operating view and
   `finance.dashboard.receivables_aging` for current/1–30/31–60/61–90/91+
   day buckets. Use `finance.tax.report` for tax-rate and withholding totals.

## Reminder boundary

A reminder candidate is only a draft for human review. Show invoice number,
due date, outstanding amount, and the selected delivery record. Request a
separate approval before preparing delivery, and never send an email, web
share, or postal request from a report operation.
