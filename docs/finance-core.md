# Finance Core

Finance Core is the source of truth for finance data. Hermes uses it through a
local MCP server; the LLM is responsible for natural-language intake,
candidate construction, validation guidance, and approval prompts only.

Finance Core owns the Finance DB, immutable invoice documents, deterministic
tax calculation, invoice numbering, state transitions, master snapshots, and
audit events. It does not write customer or bank information to Hermes
Memory, GBrain, or ordinary logs.

## Local MCP configuration

Do not edit the live profile automatically. After reviewing the server and
choosing the correct Python environment, add a local MCP entry similar to:

```yaml
mcp_servers:
  finance-core:
    command: /path/to/python
    args: ["-m", "finance_core.mcp_server"]
    cwd: /path/to/hermes-agent
    timeout: 120
    connect_timeout: 10
    session_identity:
      source: HERMES_SESSION_USER_ID
      meta_key: hermes_session_user_id
      argument_fields:
         [approved_by, issued_by, imported_by, prepared_by, updated_by,
         created_by, converted_by, corrected_by, archived_by, configured_by,
         closed_by, reopened_by, retried_by, sent_by, added_by, synced_by,
         linked_by, completed_by, cancelled_by, rejected_by, finalized_by,
         merged_by, recorded_by, revoked_by, accessed_by]
```

The `session_identity` block is required for approval and write operations.
Hermes attaches the task-local session user to each MCP request and
overwrites the corresponding write argument before transport. Finance Core
also verifies the same identity from request metadata, so a model-provided
actor label cannot authorize a different user.

The Finance DB and Document Store default to the active `HERMES_HOME` profile
under `finance-core/`. Keep that directory private. Finance Core can also be
constructed directly with a profile-specific home in tests or a supervisor.

## MCP boundary

The current MCP surface includes:

- `finance.customer.create`, `finance.customer.get`, `finance.customer.search`,
  `finance.customer.update`, `finance.customer.archive`,
  `finance.customer.revisions`, `finance.customer.get_sensitive`
- `finance.customer.contact_create`, `finance.customer.contact_list`,
  `finance.customer.contact_update`, `finance.customer.contact_archive`
- `finance.customer.lifecycle_update`,
  `finance.customer.payment_settings_update`
- `finance.customer.relationship_add`, `finance.customer.relationship_list`,
  `finance.customer.duplicate_suggest`, `finance.customer.merge`
- `finance.customer.activity_record`, `finance.customer.activity_list`,
  `finance.customer.dashboard`, `finance.customer.access_events`
- `finance.customer.import_csv`
- `finance.customer.portal_token_create`,
  `finance.customer.portal_token_revoke`,
  `finance.customer.portal_invoice_list`,
  `finance.customer.portal_invoice_get`,
  `finance.customer.portal_invoice_download`
- `finance.contract.create`, `finance.contract.get`, `finance.contract.list`
- `finance.billing_rule.create`, `finance.billing_rule.get`,
  `finance.billing_rule.list`
- `finance.approval.bootstrap_self`
- `finance.approval.policy_set`, `finance.approval.policy_list`
- `finance.period.close`, `finance.period.reopen`, `finance.period.status`,
  `finance.period.list`
- `finance.issuer.create`
- `finance.template.list_versions`
- `finance.invoice.draft_create`
- `finance.invoice.draft_from_rule`
- `finance.invoice.convert`, `finance.invoice.correct`
- `finance.invoice.validate`
- `finance.invoice.preview_pdf`
- `finance.invoice.approve`
- `finance.invoice.issue`
- `finance.invoice.get`, `finance.invoice.audit_events`
- `finance.invoice.bulk_draft_create` (stable `billing_key` makes reruns idempotent)
- `finance.invoice.draft_from_schedule` (completed and approved work from the
  named invoice calendar only)
- `finance.invoice.search`
- `finance.work.record`, `finance.work.list`, `finance.work.import_csv`,
  `finance.work.draft_from_entries`, `finance.work.from_schedule`,
  `finance.work.approve`, `finance.work.reject`
- `finance.calendar.connection_create`, `finance.calendar.connection_list`,
  `finance.calendar.connection_get`, `finance.calendar.sync`
- `finance.schedule.create`, `finance.schedule.list`, `finance.schedule.get`,
  `finance.schedule.link`, `finance.schedule.complete`,
  `finance.schedule.cancel`
- `finance.billing_run.preview`, `finance.billing_run.finalize`,
  `finance.billing_run.get`, `finance.billing_run.list`
- `finance.invoice.draft_from_billing_run`
- `finance.receivables.overdue_list`, `finance.receivables.reminder_candidates`
- `finance.revenue.summary`, `finance.revenue.by_customer`,
  `finance.revenue.by_month`, `finance.revenue.by_project`
- `finance.sales.record`, `finance.sales.search`
- `finance.dashboard.summary`, `finance.dashboard.receivables_aging`
- `finance.tax.report`
- `finance.delivery.prepare`, `finance.delivery.update_status`,
  `finance.delivery.list`, `finance.delivery.outbox`,
  `finance.delivery.retry`, `finance.delivery.dispatch_email`,
  `finance.delivery.dispatch_discord`,
  `finance.delivery.record_download`
- `finance.accounting.export_csv`, `finance.accounting.export_bundle`
- `finance.accounting.freee_invoice_preview` (official freee請求書 API payload
  preview; network-disabled)
- `finance.payment.import_csv` (preview by default; `apply=true` is an explicit write)
- `finance.payment.match_suggest`
- `finance.payment.allocate`
- `finance.payment.unallocated_list`, `finance.payment.check`
- `finance.payment.alias_add`, `finance.payment.alias_list`

Drafts have no formal invoice number and render with a `DRAFT` watermark.
`finance.invoice.issue` is separate from external delivery and stores the
canonical JSON, issued PDF, SHA-256 hashes, template version, billing data
snapshot, and audit event. The standard Japanese template is
`invoice-standard-jp@1.0.0`. Its public MakeLeaps Excel source is recorded by
URL and SHA-256 for provenance, but the downloaded workbook is not embedded in
the repository because its sample personal fields and redistribution terms do
not belong in the runtime template. The sanitized HTML/CSS renderer is the
approved execution format.

Payment CSV import and allocation are now included in the Core. CSV writes are
atomic and idempotent by `bank_transaction_id`; matching suggestions never
allocate automatically, and allocation requires an explicit approver. Raw
payer/remittance fields remain inside the Finance DB and are redacted from
MCP responses. Approval and write tools bind the supplied actor label to the
authenticated `HERMES_SESSION_USER_ID` rather than trusting a free-form name.

Configured approval policies can restrict approval, issue, and send actions by
actor, issuer, amount limit, and creator/approver separation. A closed
`YYYY-MM` accounting period blocks new invoice, payment, revenue, and other
financial events until an explicitly audited reopen. Delivery is an outbox:
each queue record has an idempotency key, attempt count, failure reason, and a
bounded manual retry path. `finance.delivery.dispatch_email` and
`finance.delivery.dispatch_discord` can only claim a queued delivery after
authenticated send approval; email remains disabled without SMTP secrets.

The live MCP server requires at least one active approval policy. The first
policy can only bootstrap the authenticated actor as an `admin`; only an active
admin can then configure other approvers, issuers, and senders. This prevents a
Discord participant from granting approval rights to an arbitrary user.

Recurring and batch draft creation, invoice search, overdue/reminder
candidates, revenue/receivables reports, delivery history, and accounting CSV
export are also implemented. Contracts and versioned billing rules generate
deterministic, idempotent drafts; the rule, calculation expression, and
source identifiers are retained on each line. Quote, delivery-note, invoice,
receipt, and correction flows create new immutable revisions and never
overwrite an issued document. Customer changes are revisioned, while issued
invoices retain their original master snapshot. Customer management also
supports lifecycle stages, owner/tag search, billing and accounting contacts,
parent/subsidiary relationships, duplicate suggestions and atomic merge,
payment terms and credit limits, activity history, dashboard summaries,
redacted CSV preview/atomic upsert, PII access auditing, and a token-based
issued-invoice portal. Sensitive contact and billing fields are available only
through explicitly authenticated sensitive tools; default MCP responses
contain IDs and masked metadata only. A duplicate suggestion is never merged
automatically, and customer CSV import is preview-only unless an authenticated
operator explicitly sets `apply=true`.

The sales subledger records recognition date, service period, customer,
project, and source independently from invoice issue date and payment value
date. `finance.revenue.*` reports invoice/receipt facts, while
`finance.sales.*` records and searches recognized revenue. Delivery
preparation only creates a local queue record. Work entries are idempotent by
source ID and can be aggregated into a billing-rule draft; dashboard and
receivables-aging views keep revenue, receivables, cash, overdue, and
unallocated cash separate. Withholding tax is accepted only as an explicit
rate/base configuration and is included in the collectible amount and tax
report.

## Visit-pharmacy calendar billing flow

The invoice source calendar is fixed to the exact Google Calendar name
`訪問薬剤管理`. The connection stores the resolved Google calendar
ID and rejects events whose calendar ID does not match it. Google Calendar
selection uses exact-name matching first, then accepts one unique Unicode/
whitespace-normalized match (Google may trim trailing spaces), and rejects
missing or ambiguous matches. The Finance Core sync uses read-only Calendar
OAuth scope and keeps only bounded event metadata. When creating the
connection, omit `calendar_id` to resolve this name through Google Calendar; a
manually supplied ID is treated as an operator-selected ID and is still
checked on every imported event.

The billing path is deliberately gated:

1. Sync events from `訪問薬剤管理` into schedule records.
2. Map an event to a customer, contract, and billing rule.
3. Mark the visit complete with operator-entered actual start and end times.
   The elapsed interval is converted to Decimal hours; the scheduled interval
   is never used as billable time. For example, 13:00-20:00 produces `7`
   billable hours.
4. Create a pending work-entry candidate and explicitly approve it.
5. Run `finance.billing_run.preview` and review unmapped, incomplete,
   unapproved, cancelled, and already billed schedules.
6. Call `finance.billing_run.finalize` with the explicitly selected eligible
   schedule IDs.
7. Call `finance.invoice.draft_from_billing_run` for the finalized run.

Finalization stores the selected schedule/work-entry IDs and actual-time
snapshot. If those facts change before drafting, the draft is rejected and the
period must be reviewed again. A billing run and its invoice draft are
idempotent by their calendar/rule/period scope.

The resulting invoice is a DRAFT. Invoice approval, issue, preparation, and
external delivery remain separate actions. A calendar event alone never
creates or sends an invoice.

`finance.accounting.export_bundle` is a redacted CSV boundary for generic,
freee, Money Forward, and Yayoi targets. It does not call an external API or
store credentials; API adapters remain disabled until their contracts and
destination policies are explicitly approved.

`finance.accounting.freee_invoice_preview` maps an issued invoice to the
official freee請求書 API v1 request shape using explicit freee company and
partner master IDs. It records the official API/OAuth endpoints for review but
does not perform OAuth, upload a PDF, or create an external invoice. The
official endpoint is `https://api.freee.co.jp/iv/invoices`.

The Python package is organized as a small Finance Core facade plus bounded
domain services under `finance_core/domains/`: approvals, customers, delivery,
integrations, invoices, masters, payments, reporting, revenue, schedules, and
work. The facade keeps the existing MCP-facing API stable while each service owns one
business boundary. SQLite remains the transactional persistence boundary, so
invoice issue, payment allocation, and period locks retain atomic behavior
while domain code can evolve independently.

The accounting plugin registers `skill-fin-customer-management`,
`skill-fin-invoice-generation`,
`skill-fin-payment-status-check`, `skill-fin-revenue-tracking`, and
`skill-fin-spreadsheet-integration`. Skills own natural-language intake,
candidate explanation, approval prompts, and exception handling. Finance Core
owns all amounts, tax, persistence, state transitions, matching, reporting,
and audit facts. The spreadsheet skill currently uses CSV as the controlled
boundary; it does not treat an edited workbook as a source of truth.
