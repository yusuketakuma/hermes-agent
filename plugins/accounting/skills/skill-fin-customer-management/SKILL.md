---
name: skill-fin-customer-management
description: "Manage customer master data, contacts, lifecycle, matching, and portal access through Finance Core with PII boundaries."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, customer, crm, billing, privacy, 顧客管理]
    related_skills: [skill-fin-invoice-generation, skill-fin-payment-status-check, skill-fin-revenue-tracking]
---

# Finance customer management

Use Finance Core as the only source of truth for customer master data. Do not
write names, addresses, email addresses, phone numbers, contact details, portal
tokens, or billing documents to Hermes Memory, GBrain, ordinary logs, or a
skill-owned file.

## Safe workflow

1. Resolve a customer with `finance.customer.search` or `finance.customer.get`.
   Use `customer_id` in subsequent calls. Default responses intentionally omit
   billing-profile PII.
2. Create or update a customer only with explicit operator intent. Set the
   corporate number, external ID, lifecycle, owner, tags, billing profile,
   payment terms, credit limit, currency, and dunning policy from controlled
   input; never infer tax or payment settings from the customer name.
3. Use `finance.customer.contact_create` and the safe form of
   `finance.customer.contact_list` for contact records. Request sensitive
   contact fields only with an authenticated session through
   `include_sensitive=true` or `finance.customer.get_sensitive`.
4. Use `finance.customer.lifecycle_update` for lead, prospect, active,
   suspended, dormant, and closed transitions. Use
   `finance.customer.relationship_add` for parent/subsidiary or billing and
   shipping relationships.
5. Before merging records, call `finance.customer.duplicate_suggest`, present
   the reasons and candidates to a human, and wait for explicit confirmation.
   Only then call `finance.customer.merge`; it is atomic and keeps issued
   invoice history immutable.
6. For bulk maintenance, call `finance.customer.import_csv` with the redacted
   preview first. Apply only after review, using the authenticated operator
   and `apply=true`; set `upsert=true` only when replacement of matched master
   records was explicitly approved.
7. Use `finance.customer.activity_record` for non-sensitive operational notes
   and `finance.customer.dashboard` for customer-level contract, invoice,
   receivable, relationship, contact, and activity summaries.
8. Create a customer portal token only for an approved customer and expiry.
   Treat the returned token as a secret, transmit it only through the approved
   delivery channel, and use the portal tools only with the token. Never place
   it in a chat summary or log.

Customer PII access is audited by `finance.customer.access_events`. A failed
authorization, uncertain duplicate match, invalid tax/payment setting, or
missing master is an exception: stop and ask for an operator decision rather
than guessing or writing a local copy.
