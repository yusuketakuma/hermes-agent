"""Transactional Finance DB implementation."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from finance_core.errors import FinanceConflictError, FinanceNotFoundError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FinanceStore:
    """Profile-scoped SQLite source of truth; no data is written to Hermes Memory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.path = self.root / "finance.db"
        self.path.touch(mode=0o600, exist_ok=True)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS issuers (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customer_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    actor TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(customer_id, revision)
                );
                CREATE TABLE IF NOT EXISTS customer_contacts (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    active_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customer_relationships (
                    id TEXT PRIMARY KEY,
                    parent_customer_id TEXT NOT NULL,
                    child_customer_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    active_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(parent_customer_id, child_customer_id, relationship_type)
                );
                CREATE TABLE IF NOT EXISTS customer_activities (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    activity_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customer_access_events (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customer_merges (
                    id TEXT PRIMARY KEY,
                    source_customer_id TEXT NOT NULL UNIQUE,
                    target_customer_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    merged_by TEXT NOT NULL,
                    merged_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customer_portal_tokens (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_used_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_policies (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    issuer_id TEXT,
                    data TEXT NOT NULL,
                    active_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_aliases (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    alias_display TEXT NOT NULL,
                    alias_normalized TEXT NOT NULL,
                    active_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(customer_id, alias_normalized)
                );
                CREATE TABLE IF NOT EXISTS contracts (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    active_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS billing_rules (
                    id TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    active_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_entries (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL UNIQUE,
                    customer_id TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    billing_rule_id TEXT NOT NULL,
                    service_date TEXT NOT NULL,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    billed_invoice_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calendar_connections (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    calendar_id TEXT NOT NULL,
                    sync_token TEXT,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, calendar_id)
                );
                CREATE TABLE IF NOT EXISTS schedule_events (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT,
                    contract_id TEXT,
                    billing_rule_id TEXT,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS billing_runs (
                    id TEXT PRIMARY KEY,
                    calendar_connection_id TEXT NOT NULL,
                    billing_rule_id TEXT NOT NULL,
                    service_period TEXT NOT NULL,
                    billing_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(calendar_connection_id, billing_rule_id, service_period)
                );
                CREATE INDEX IF NOT EXISTS schedule_events_time_idx
                    ON schedule_events(starts_at, ends_at, status);
                CREATE INDEX IF NOT EXISTS schedule_events_customer_idx
                    ON schedule_events(customer_id, starts_at);
                CREATE INDEX IF NOT EXISTS billing_runs_status_idx
                    ON billing_runs(status, service_period);
                CREATE TABLE IF NOT EXISTS calendar_event_links (
                    id TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL,
                    external_event_id TEXT NOT NULL,
                    occurrence_start TEXT NOT NULL DEFAULT '',
                    schedule_id TEXT NOT NULL,
                    etag TEXT,
                    data TEXT NOT NULL,
                    deleted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(connection_id, external_event_id, occurrence_start)
                );
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    invoice_number TEXT UNIQUE,
                    data TEXT NOT NULL,
                    document_status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invoice_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    actor TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(invoice_id, revision)
                );
                CREATE TABLE IF NOT EXISTS invoice_sequences (
                    period TEXT PRIMARY KEY,
                    next_number INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    invoice_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id TEXT,
                    action TEXT NOT NULL,
                    actor TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS period_locks (
                    period TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_import_batches (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    bank_transaction_id TEXT NOT NULL UNIQUE,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_allocations (
                    id TEXT PRIMARY KEY,
                    payment_id TEXT NOT NULL,
                    invoice_id TEXT NOT NULL,
                    allocated_amount TEXT NOT NULL,
                    allocation_method TEXT NOT NULL,
                    matched_reason TEXT,
                    approved_by TEXT NOT NULL,
                    allocated_at TEXT NOT NULL,
                    UNIQUE(payment_id, invoice_id)
                );
                CREATE TABLE IF NOT EXISTS delivery_records (
                    id TEXT PRIMARY KEY,
                    invoice_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    available_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revenue_entries (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    recognition_date TEXT NOT NULL,
                    project_id TEXT,
                    department_id TEXT,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, source_id)
                );
                CREATE INDEX IF NOT EXISTS invoice_status_idx ON invoices(document_status);
                CREATE INDEX IF NOT EXISTS customer_revision_idx ON customer_revisions(customer_id, revision);
                CREATE INDEX IF NOT EXISTS customer_contact_idx ON customer_contacts(customer_id, active_status);
                CREATE INDEX IF NOT EXISTS customer_relationship_parent_idx ON customer_relationships(parent_customer_id, active_status);
                CREATE INDEX IF NOT EXISTS customer_relationship_child_idx ON customer_relationships(child_customer_id, active_status);
                CREATE INDEX IF NOT EXISTS customer_activity_idx ON customer_activities(customer_id, occurred_at);
                CREATE INDEX IF NOT EXISTS customer_access_idx ON customer_access_events(customer_id, created_at);
                CREATE INDEX IF NOT EXISTS customer_portal_customer_idx ON customer_portal_tokens(customer_id, expires_at);
                CREATE INDEX IF NOT EXISTS approval_policy_actor_idx ON approval_policies(actor_id, active_status);
                CREATE INDEX IF NOT EXISTS payment_alias_customer_idx ON payment_aliases(customer_id, active_status);
                CREATE INDEX IF NOT EXISTS period_lock_status_idx ON period_locks(status, period);
                CREATE INDEX IF NOT EXISTS contract_customer_idx ON contracts(customer_id, active_status);
                CREATE INDEX IF NOT EXISTS billing_rule_contract_idx ON billing_rules(contract_id, active_status);
                CREATE INDEX IF NOT EXISTS work_entry_period_idx ON work_entries(service_date, status);
                CREATE INDEX IF NOT EXISTS work_entry_rule_idx ON work_entries(billing_rule_id, service_date, status);
                CREATE INDEX IF NOT EXISTS audit_invoice_idx ON audit_events(invoice_id, id);
                CREATE INDEX IF NOT EXISTS payment_status_idx ON payments(status);
                CREATE INDEX IF NOT EXISTS allocation_invoice_idx ON payment_allocations(invoice_id);
                CREATE INDEX IF NOT EXISTS delivery_invoice_idx ON delivery_records(invoice_id, created_at);
                CREATE INDEX IF NOT EXISTS delivery_status_idx ON delivery_records(status);
                CREATE INDEX IF NOT EXISTS delivery_outbox_status_idx ON delivery_outbox(status, available_at);
                CREATE INDEX IF NOT EXISTS revenue_date_idx ON revenue_entries(recognition_date, status);
                CREATE INDEX IF NOT EXISTS revenue_customer_idx ON revenue_entries(customer_id, recognition_date);
                CREATE INDEX IF NOT EXISTS revenue_project_idx ON revenue_entries(project_id, recognition_date);
                """
            )

    @staticmethod
    def _decode(raw: str) -> dict[str, Any]:
        return json.loads(raw)

    @staticmethod
    def _encode(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _put_master(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        record = copy.deepcopy(record)
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute(
                f"INSERT INTO {table} (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (record["id"], self._encode(record), record["created_at"], record["updated_at"]),
            )
        return record

    def put_issuer(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._put_master("issuers", record)

    def put_customer(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._put_master("customers", record)

    def get_issuer(self, record_id: str) -> dict[str, Any] | None:
        return self._get_master("issuers", record_id)

    def get_customer(self, record_id: str) -> dict[str, Any] | None:
        return self._get_master("customers", record_id)

    def record_audit(
        self,
        action: str,
        *,
        actor: str | None,
        detail: dict[str, Any],
        invoice_id: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            self._insert_audit(connection, invoice_id, action, actor, detail)

    def list_customers(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM customers ORDER BY json_extract(data, '$.legal_name'), id"
            ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def put_customer_contact(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO customer_contacts (id, customer_id, data, active_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["customer_id"],
                    self._encode(stored),
                    stored["active_status"],
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
        return copy.deepcopy(stored)

    def get_customer_contact(self, contact_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM customer_contacts WHERE id = ?", (str(contact_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_customer_contacts(
        self,
        customer_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT data FROM customer_contacts WHERE customer_id = ?"
        params: list[Any] = [str(customer_id)]
        if not include_inactive:
            query += " AND active_status = 'ACTIVE'"
        query += " ORDER BY json_extract(data, '$.is_primary') DESC, created_at, id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def update_customer_contact(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            result = connection.execute(
                "UPDATE customer_contacts SET data = ?, active_status = ?, updated_at = ? WHERE id = ?",
                (
                    self._encode(stored),
                    stored["active_status"],
                    now,
                    stored["id"],
                ),
            )
            if result.rowcount != 1:
                raise FinanceNotFoundError(f"Customer contact not found: {stored['id']}")
        return copy.deepcopy(stored)

    def put_customer_relationship(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO customer_relationships "
                "(id, parent_customer_id, child_customer_id, relationship_type, data, active_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(parent_customer_id, child_customer_id, relationship_type) DO UPDATE SET "
                "data = excluded.data, active_status = excluded.active_status, updated_at = excluded.updated_at",
                (
                    stored["id"],
                    stored["parent_customer_id"],
                    stored["child_customer_id"],
                    stored["relationship_type"],
                    self._encode(stored),
                    stored["active_status"],
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
        return copy.deepcopy(stored)

    def list_customer_relationships(
        self,
        customer_id: str,
        *,
        relationship_type: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT data FROM customer_relationships "
            "WHERE (parent_customer_id = ? OR child_customer_id = ?)"
        )
        params: list[Any] = [str(customer_id), str(customer_id)]
        if relationship_type:
            query += " AND relationship_type = ?"
            params.append(str(relationship_type))
        if not include_inactive:
            query += " AND active_status = 'ACTIVE'"
        query += " ORDER BY relationship_type, id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def put_customer_activity(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored.setdefault("occurred_at", now)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO customer_activities "
                "(id, customer_id, activity_type, data, actor, occurred_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["customer_id"],
                    stored["activity_type"],
                    self._encode(stored),
                    stored["actor"],
                    stored["occurred_at"],
                    stored["created_at"],
                ),
            )
        return copy.deepcopy(stored)

    def list_customer_activities(
        self,
        customer_id: str,
        *,
        activity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT data FROM customer_activities WHERE customer_id = ?"
        params: list[Any] = [str(customer_id)]
        if activity_type:
            query += " AND activity_type = ?"
            params.append(str(activity_type))
        query += " ORDER BY occurred_at DESC, id DESC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def record_customer_access(self, record: dict[str, Any]) -> None:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO customer_access_events "
                "(id, customer_id, action, actor, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["customer_id"],
                    stored["action"],
                    stored["actor"],
                    self._encode(stored),
                    stored["created_at"],
                ),
            )

    def list_customer_access_events(self, customer_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM customer_access_events WHERE customer_id = "
                "? ORDER BY created_at, id",
                (str(customer_id),),
            ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def put_customer_portal_token(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO customer_portal_tokens "
                "(id, customer_id, token_hash, expires_at, revoked_at, last_used_at, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["customer_id"],
                    stored["token_hash"],
                    stored["expires_at"],
                    stored.get("revoked_at"),
                    stored.get("last_used_at"),
                    stored["created_by"],
                    stored["created_at"],
                ),
            )
        return copy.deepcopy(stored)

    def get_customer_portal_token(self, token_hash: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, customer_id, token_hash, expires_at, revoked_at, last_used_at, created_by, created_at "
                "FROM customer_portal_tokens WHERE token_hash = ?",
                (str(token_hash),),
            ).fetchone()
        return dict(row) if row else None

    def touch_customer_portal_token(self, token_id: str, *, used_at: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE customer_portal_tokens SET last_used_at = ? WHERE id = ?",
                (str(used_at), str(token_id)),
            )

    def revoke_customer_portal_token(self, token_id: str, *, revoked_at: str) -> None:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                "UPDATE customer_portal_tokens SET revoked_at = ? WHERE id = ?",
                (str(revoked_at), str(token_id)),
            )
            if result.rowcount != 1:
                raise FinanceNotFoundError(f"Customer portal token not found: {token_id}")

    def put_approval_policy(self, record: dict[str, Any]) -> dict[str, Any]:
        """Upsert one actor/scope policy without retaining stale permissions."""

        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM approval_policies WHERE actor_id = ? AND COALESCE(issuer_id, '') = COALESCE(?, '')",
                (stored["actor_id"], stored.get("issuer_id")),
            )
            connection.execute(
                "INSERT INTO approval_policies (id, actor_id, issuer_id, data, active_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["actor_id"],
                    stored.get("issuer_id"),
                    self._encode(stored),
                    stored["active_status"],
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
            connection.commit()
        return copy.deepcopy(stored)

    def list_approval_policies(self, *, actor_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT data FROM approval_policies WHERE active_status = 'ACTIVE'"
        params: tuple[Any, ...] = ()
        if actor_id:
            query += " AND actor_id = ?"
            params = (str(actor_id),)
        query += " ORDER BY actor_id, issuer_id, id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def put_payment_alias(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO payment_aliases "
                "(id, customer_id, alias_display, alias_normalized, active_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(customer_id, alias_normalized) DO UPDATE SET alias_display = excluded.alias_display, "
                "active_status = excluded.active_status, updated_at = excluded.updated_at",
                (
                    stored["id"],
                    stored["customer_id"],
                    stored["alias_display"],
                    stored["alias_normalized"],
                    stored["active_status"],
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
        return copy.deepcopy(stored)

    def list_payment_aliases(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id, customer_id, alias_display, alias_normalized, active_status, created_at, updated_at FROM payment_aliases WHERE active_status = 'ACTIVE'"
        params: tuple[Any, ...] = ()
        if customer_id:
            query += " AND customer_id = ?"
            params = (str(customer_id),)
        query += " ORDER BY customer_id, alias_normalized"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_period_lock(self, period: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM period_locks WHERE period = ?", (str(period),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_period_locks(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM period_locks ORDER BY period"
            ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def close_period(self, period: str, *, actor: str, reason: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data, created_at FROM period_locks WHERE period = ?", (str(period),)
            ).fetchone()
            if row:
                current = self._decode(row["data"])
                if current.get("status") == "CLOSED":
                    connection.commit()
                    return current
                created_at = row["created_at"]
            else:
                current = {"period": str(period)}
                created_at = now
            current.update(
                {
                    "period": str(period),
                    "status": "CLOSED",
                    "closed_by": actor,
                    "closed_at": now,
                    "reason": reason,
                }
            )
            connection.execute(
                "INSERT INTO period_locks (period, data, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(period) DO UPDATE SET data = excluded.data, status = excluded.status, updated_at = excluded.updated_at",
                (str(period), self._encode(current), "CLOSED", created_at, now),
            )
            connection.commit()
        return copy.deepcopy(current)

    def reopen_period(self, period: str, *, actor: str, reason: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data, created_at FROM period_locks WHERE period = ?", (str(period),)
            ).fetchone()
            current = self._decode(row["data"]) if row else {"period": str(period)}
            current.update(
                {
                    "period": str(period),
                    "status": "OPEN",
                    "reopened_by": actor,
                    "reopened_at": now,
                    "reason": reason,
                }
            )
            connection.execute(
                "INSERT INTO period_locks (period, data, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(period) DO UPDATE SET data = excluded.data, status = excluded.status, updated_at = excluded.updated_at",
                (str(period), self._encode(current), "OPEN", row["created_at"] if row else now, now),
            )
            connection.commit()
        return copy.deepcopy(current)

    def update_customer(
        self,
        record: dict[str, Any],
        *,
        actor: str,
        action: str = "customer.update",
    ) -> dict[str, Any]:
        """Update a customer while retaining an immutable revision history."""

        now = utc_now_iso()
        stored = copy.deepcopy(record)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM customers WHERE id = ?", (str(stored["id"]),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Customer not found: {stored['id']}")
            current = self._decode(row["data"])
            expected_revision = int(current.get("revision", 0))
            supplied_revision = int(stored.get("revision", expected_revision + 1))
            if supplied_revision != expected_revision + 1:
                connection.rollback()
                raise FinanceConflictError("Customer revision is stale or invalid")
            stored["created_at"] = current.get("created_at", now)
            stored["updated_at"] = now
            stored["revision"] = supplied_revision
            connection.execute(
                "UPDATE customers SET data = ?, updated_at = ? WHERE id = ?",
                (self._encode(stored), now, stored["id"]),
            )
            connection.execute(
                "INSERT INTO customer_revisions (customer_id, revision, data, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (stored["id"], supplied_revision, self._encode(stored), actor, now),
            )
            self._insert_audit(
                connection,
                None,
                action,
                actor,
                {"customer_id": stored["id"], "revision": supplied_revision},
            )
            connection.commit()
        return copy.deepcopy(stored)

    def list_customer_revisions(self, customer_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT revision, data, actor, created_at FROM customer_revisions "
                "WHERE customer_id = ? ORDER BY revision",
                (str(customer_id),),
            ).fetchall()
        return [
            {
                "customer_id": customer_id,
                "revision": int(row["revision"]),
                "data": self._decode(row["data"]),
                "actor": row["actor"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def import_customers_batch(
        self,
        new_records: list[dict[str, Any]],
        updated_records: list[dict[str, Any]],
        *,
        actor: str,
    ) -> dict[str, int]:
        """Apply a customer CSV batch atomically, including revision checks."""

        now = utc_now_iso()
        inserted = 0
        updated_count = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for raw in new_records:
                    record = copy.deepcopy(raw)
                    record.setdefault("created_at", now)
                    record["updated_at"] = now
                    connection.execute(
                        "INSERT INTO customers (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (
                            record["id"],
                            self._encode(record),
                            record["created_at"],
                            record["updated_at"],
                        ),
                    )
                    self._insert_audit(
                        connection,
                        None,
                        "customer.import",
                        actor,
                        {"customer_id": record["id"]},
                    )
                    inserted += 1
                for raw in updated_records:
                    record = copy.deepcopy(raw)
                    row = connection.execute(
                        "SELECT data FROM customers WHERE id = ?", (str(record["id"]),)
                    ).fetchone()
                    if row is None:
                        raise FinanceNotFoundError(f"Customer not found: {record['id']}")
                    current = self._decode(row["data"])
                    expected = int(current.get("revision", 0)) + 1
                    if int(record.get("revision", 0)) != expected:
                        raise FinanceConflictError("Customer revision is stale or invalid")
                    record["created_at"] = current.get("created_at", now)
                    record["updated_at"] = now
                    connection.execute(
                        "UPDATE customers SET data = ?, updated_at = ? WHERE id = ?",
                        (self._encode(record), now, record["id"]),
                    )
                    connection.execute(
                        "INSERT INTO customer_revisions (customer_id, revision, data, actor, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            record["id"],
                            int(record["revision"]),
                            self._encode(record),
                            actor,
                            now,
                        ),
                    )
                    self._insert_audit(
                        connection,
                        None,
                        "customer.import_update",
                        actor,
                        {"customer_id": record["id"], "revision": int(record["revision"])},
                    )
                    updated_count += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"inserted_count": inserted, "updated_count": updated_count}

    def merge_customers(
        self,
        source_customer_id: str,
        target_customer_id: str,
        *,
        merged_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """Merge customer references atomically while preserving issued history."""

        source_id = str(source_customer_id)
        target_id = str(target_customer_id)
        if source_id == target_id:
            raise FinanceConflictError("A customer cannot be merged into itself")
        now = utc_now_iso()
        migrated = {"contracts": 0, "work_entries": 0, "revenue_entries": 0, "draft_invoices": 0}
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                source_row = connection.execute(
                    "SELECT data FROM customers WHERE id = ?", (source_id,)
                ).fetchone()
                target_row = connection.execute(
                    "SELECT data FROM customers WHERE id = ?", (target_id,)
                ).fetchone()
                if source_row is None or target_row is None:
                    missing = source_id if source_row is None else target_id
                    raise FinanceNotFoundError(f"Customer not found: {missing}")
                source = self._decode(source_row["data"])
                target = self._decode(target_row["data"])
                if source.get("merged_into"):
                    raise FinanceConflictError("Source customer has already been merged")
                if target.get("active_status") != "ACTIVE":
                    raise FinanceConflictError("Target customer must be active")

                target_aliases = list(target.get("aliases") or [])
                source_profile = source.get("billing_profile") or {}
                for alias in [
                    source.get("legal_name"),
                    source_profile.get("billing_name"),
                    *(source.get("aliases") or []),
                ]:
                    if alias and alias not in target_aliases and len(target_aliases) < 100:
                        target_aliases.append(alias)
                target_tags = list(target.get("tags") or [])
                for tag in source.get("tags") or []:
                    if tag not in target_tags and len(target_tags) < 100:
                        target_tags.append(tag)
                target["aliases"] = target_aliases
                target["tags"] = target_tags
                merged_ids = list(target.get("merged_customer_ids") or [])
                if source_id not in merged_ids:
                    merged_ids.append(source_id)
                target["merged_customer_ids"] = merged_ids
                target_revision = int(target.get("revision", 0)) + 1
                target["revision"] = target_revision
                target["updated_at"] = now

                source["active_status"] = "INACTIVE"
                source["lifecycle_status"] = "CLOSED"
                source["merged_into"] = target_id
                source_revision = int(source.get("revision", 0)) + 1
                source["revision"] = source_revision
                source["updated_at"] = now

                connection.execute(
                    "UPDATE customers SET data = ?, updated_at = ? WHERE id = ?",
                    (self._encode(target), now, target_id),
                )
                connection.execute(
                    "UPDATE customers SET data = ?, updated_at = ? WHERE id = ?",
                    (self._encode(source), now, source_id),
                )
                for record in (target, source):
                    connection.execute(
                        "INSERT INTO customer_revisions (customer_id, revision, data, actor, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (record["id"], int(record["revision"]), self._encode(record), merged_by, now),
                    )

                for table, column, counter in (
                    ("contracts", "customer_id", "contracts"),
                    ("work_entries", "customer_id", "work_entries"),
                    ("revenue_entries", "customer_id", "revenue_entries"),
                ):
                    rows = connection.execute(
                        f"SELECT id, data FROM {table} WHERE {column} = ?", (source_id,)
                    ).fetchall()
                    for row in rows:
                        record = self._decode(row["data"])
                        record[column] = target_id
                        record["updated_at"] = now
                        connection.execute(
                            f"UPDATE {table} SET {column} = ?, data = ?, updated_at = ? WHERE id = ?",
                            (target_id, self._encode(record), now, row["id"]),
                        )
                        migrated[counter] += 1

                invoice_rows = connection.execute(
                    "SELECT id, data, document_status FROM invoices WHERE document_status IN ('DRAFT', 'APPROVED')"
                ).fetchall()
                for row in invoice_rows:
                    record = self._decode(row["data"])
                    if record.get("customer_id") != source_id:
                        continue
                    record["customer_id"] = target_id
                    record["updated_at"] = now
                    connection.execute(
                        "UPDATE invoices SET data = ?, updated_at = ? WHERE id = ?",
                        (self._encode(record), now, row["id"]),
                    )
                    migrated["draft_invoices"] += 1

                alias_rows = connection.execute(
                    "SELECT alias_display, alias_normalized, active_status, created_at, updated_at "
                    "FROM payment_aliases WHERE customer_id = ?",
                    (source_id,),
                ).fetchall()
                for row in alias_rows:
                    connection.execute(
                        "INSERT OR IGNORE INTO payment_aliases "
                        "(id, customer_id, alias_display, alias_normalized, active_status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"pal_{uuid.uuid4().hex}",
                            target_id,
                            row["alias_display"],
                            row["alias_normalized"],
                            row["active_status"],
                            row["created_at"],
                            now,
                        ),
                    )
                connection.execute("DELETE FROM payment_aliases WHERE customer_id = ?", (source_id,))
                contact_rows = connection.execute(
                    "SELECT id, data FROM customer_contacts WHERE customer_id = ?",
                    (source_id,),
                ).fetchall()
                for row in contact_rows:
                    contact = self._decode(row["data"])
                    contact["customer_id"] = target_id
                    contact["updated_at"] = now
                    connection.execute(
                        "UPDATE customer_contacts SET customer_id = ?, data = ?, updated_at = ? WHERE id = ?",
                        (target_id, self._encode(contact), now, row["id"]),
                    )
                connection.execute(
                    "UPDATE customer_portal_tokens SET customer_id = ? WHERE customer_id = ?",
                    (target_id, source_id),
                )

                relationship_rows = connection.execute(
                    "SELECT id, parent_customer_id, child_customer_id, relationship_type, data "
                    "FROM customer_relationships WHERE parent_customer_id = ? OR child_customer_id = ?",
                    (source_id, source_id),
                ).fetchall()
                for row in relationship_rows:
                    parent_id = target_id if row["parent_customer_id"] == source_id else row["parent_customer_id"]
                    child_id = target_id if row["child_customer_id"] == source_id else row["child_customer_id"]
                    if parent_id == child_id:
                        connection.execute("DELETE FROM customer_relationships WHERE id = ?", (row["id"],))
                        continue
                    record = self._decode(row["data"])
                    record["parent_customer_id"] = parent_id
                    record["child_customer_id"] = child_id
                    record["updated_at"] = now
                    try:
                        connection.execute(
                            "UPDATE customer_relationships SET parent_customer_id = ?, child_customer_id = ?, "
                            "data = ?, updated_at = ? WHERE id = ?",
                            (parent_id, child_id, self._encode(record), now, row["id"]),
                        )
                    except sqlite3.IntegrityError:
                        connection.execute("DELETE FROM customer_relationships WHERE id = ?", (row["id"],))

                merge_record = {
                    "id": f"cmerge_{uuid.uuid4().hex}",
                    "source_customer_id": source_id,
                    "target_customer_id": target_id,
                    "reason": reason,
                    "migrated": migrated,
                }
                connection.execute(
                    "INSERT INTO customer_merges "
                    "(id, source_customer_id, target_customer_id, data, merged_by, merged_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        merge_record["id"],
                        source_id,
                        target_id,
                        self._encode(merge_record),
                        merged_by,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    None,
                    "customer.merge",
                    merged_by,
                    {"source_customer_id": source_id, "target_customer_id": target_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "source_customer_id": source_id,
            "target_customer_id": target_id,
            "migrated": migrated,
            "source": copy.deepcopy(source),
            "target": copy.deepcopy(target),
        }

    def put_contract(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._put_entity("contracts", record, status_key="active_status")

    def get_contract(self, record_id: str) -> dict[str, Any] | None:
        return self._get_entity("contracts", record_id)

    def list_contracts(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        return self._list_entities("contracts", customer_id=customer_id, foreign_key="customer_id")

    def put_billing_rule(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._put_entity("billing_rules", record, status_key="active_status")

    def get_billing_rule(self, record_id: str) -> dict[str, Any] | None:
        return self._get_entity("billing_rules", record_id)

    def list_billing_rules(self, contract_id: str | None = None) -> list[dict[str, Any]]:
        return self._list_entities("billing_rules", contract_id=contract_id, foreign_key="contract_id")

    def put_calendar_connection(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO calendar_connections "
                    "(id, provider, calendar_id, sync_token, status, data, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stored["id"],
                        stored["provider"],
                        stored["calendar_id"],
                        stored.get("sync_token"),
                        stored["status"],
                        self._encode(stored),
                        stored["created_at"],
                        stored["updated_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "calendar_connections.provider" in str(exc):
                    raise FinanceConflictError("A calendar connection already exists") from exc
                raise
        return copy.deepcopy(stored)

    def get_calendar_connection(self, connection_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM calendar_connections WHERE id = ?", (str(connection_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_calendar_connections(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM calendar_connections ORDER BY provider, calendar_id, id"
            ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def update_calendar_connection(
        self,
        connection_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM calendar_connections WHERE id = ?", (str(connection_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Calendar connection not found: {connection_id}")
            record = self._decode(row["data"])
            record.update(copy.deepcopy(updates))
            record["updated_at"] = now
            connection.execute(
                "UPDATE calendar_connections SET sync_token = ?, status = ?, data = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    record.get("sync_token"),
                    record["status"],
                    self._encode(record),
                    now,
                    str(connection_id),
                ),
            )
            connection.commit()
        return copy.deepcopy(record)

    def put_schedule_event(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO schedule_events "
                "(id, customer_id, contract_id, billing_rule_id, starts_at, ends_at, status, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored.get("customer_id"),
                    stored.get("contract_id"),
                    stored.get("billing_rule_id"),
                    stored["starts_at"],
                    stored["ends_at"],
                    stored["status"],
                    self._encode(stored),
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
        return copy.deepcopy(stored)

    def get_schedule_event(self, schedule_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM schedule_events WHERE id = ?", (str(schedule_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_schedule_events(
        self,
        *,
        customer_id: str | None = None,
        status: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(str(customer_id))
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if from_time:
            clauses.append("ends_at >= ?")
            params.append(str(from_time))
        if to_time:
            clauses.append("starts_at <= ?")
            params.append(str(to_time))
        query = "SELECT data FROM schedule_events WHERE " + " AND ".join(clauses)
        query += " ORDER BY starts_at, id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def update_schedule_event(
        self,
        schedule_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM schedule_events WHERE id = ?", (str(schedule_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Schedule event not found: {schedule_id}")
            record = self._decode(row["data"])
            record.update(copy.deepcopy(updates))
            record["updated_at"] = now
            connection.execute(
                "UPDATE schedule_events SET customer_id = ?, contract_id = ?, billing_rule_id = ?, "
                "starts_at = ?, ends_at = ?, status = ?, data = ?, updated_at = ? WHERE id = ?",
                (
                    record.get("customer_id"),
                    record.get("contract_id"),
                    record.get("billing_rule_id"),
                    record["starts_at"],
                    record["ends_at"],
                    record["status"],
                    self._encode(record),
                    now,
                    str(schedule_id),
                ),
            )
            connection.commit()
        return copy.deepcopy(record)

    def put_billing_run(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO billing_runs "
                    "(id, calendar_connection_id, billing_rule_id, service_period, billing_key, status, data, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stored["id"],
                        stored["calendar_connection_id"],
                        stored["billing_rule_id"],
                        stored["service_period"],
                        stored["billing_key"],
                        stored["status"],
                        self._encode(stored),
                        stored["created_at"],
                        stored["updated_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "billing_runs.billing_key" in str(exc):
                    raise FinanceConflictError("A billing run with the same billing_key already exists") from exc
                if "billing_runs.calendar_connection_id" in str(exc):
                    raise FinanceConflictError("A billing run already exists for this calendar, rule, and period") from exc
                raise
        return copy.deepcopy(stored)

    def get_billing_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM billing_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def find_billing_run_by_scope(
        self,
        *,
        calendar_connection_id: str,
        billing_rule_id: str,
        service_period: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM billing_runs "
                "WHERE calendar_connection_id = ? AND billing_rule_id = ? AND service_period = ?",
                (str(calendar_connection_id), str(billing_rule_id), str(service_period)),
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_billing_runs(
        self,
        *,
        calendar_connection_id: str | None = None,
        service_period: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if calendar_connection_id:
            clauses.append("calendar_connection_id = ?")
            params.append(str(calendar_connection_id))
        if service_period:
            clauses.append("service_period = ?")
            params.append(str(service_period))
        query = "SELECT data FROM billing_runs WHERE " + " AND ".join(clauses)
        query += " ORDER BY service_period, id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def update_billing_run(
        self,
        run_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM billing_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Billing run not found: {run_id}")
            record = self._decode(row["data"])
            record.update(copy.deepcopy(updates))
            record["updated_at"] = now
            connection.execute(
                "UPDATE billing_runs SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                (
                    record["status"],
                    self._encode(record),
                    now,
                    str(run_id),
                ),
            )
            connection.commit()
        return copy.deepcopy(record)

    def find_schedule_event_by_work_entry_id(self, work_entry_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT data FROM schedule_events").fetchall()
        for row in rows:
            record = self._decode(row["data"])
            if record.get("work_entry_id") == str(work_entry_id):
                return record
        return None

    def put_calendar_event_link(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("occurrence_start", "")
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO calendar_event_links "
                "(id, connection_id, external_event_id, occurrence_start, schedule_id, etag, data, deleted_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["connection_id"],
                    stored["external_event_id"],
                    stored["occurrence_start"],
                    stored["schedule_id"],
                    stored.get("etag"),
                    self._encode(stored),
                    stored.get("deleted_at"),
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
        return copy.deepcopy(stored)

    def get_calendar_event_link(
        self,
        connection_id: str,
        external_event_id: str,
        occurrence_start: str = "",
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM calendar_event_links "
                "WHERE connection_id = ? AND external_event_id = ? AND occurrence_start = ?",
                (str(connection_id), str(external_event_id), str(occurrence_start or "")),
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def update_calendar_event_link(
        self,
        link_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM calendar_event_links WHERE id = ?", (str(link_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Calendar event link not found: {link_id}")
            record = self._decode(row["data"])
            record.update(copy.deepcopy(updates))
            record["updated_at"] = now
            connection.execute(
                "UPDATE calendar_event_links SET etag = ?, data = ?, deleted_at = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    record.get("etag"),
                    self._encode(record),
                    record.get("deleted_at"),
                    now,
                    str(link_id),
                ),
            )
            connection.commit()
        return copy.deepcopy(record)

    def find_work_entry_by_source_id(self, source_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM work_entries WHERE source_id = ?", (str(source_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def get_work_entry(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM work_entries WHERE id = ?", (str(entry_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def update_work_entry(self, entry_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM work_entries WHERE id = ?", (str(entry_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Work entry not found: {entry_id}")
            record = self._decode(row["data"])
            record.update(copy.deepcopy(updates))
            record["updated_at"] = now
            connection.execute(
                "UPDATE work_entries SET data = ?, status = ?, billed_invoice_id = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    self._encode(record),
                    record["status"],
                    record.get("billed_invoice_id"),
                    now,
                    str(entry_id),
                ),
            )
            connection.commit()
        return copy.deepcopy(record)

    def put_work_entry(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO work_entries "
                    "(id, source_id, customer_id, contract_id, billing_rule_id, service_date, data, status, billed_invoice_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stored["id"],
                        stored["source_id"],
                        stored["customer_id"],
                        stored["contract_id"],
                        stored["billing_rule_id"],
                        stored["service_date"],
                        self._encode(stored),
                        stored["status"],
                        stored.get("billed_invoice_id"),
                        stored["created_at"],
                        stored["updated_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "source_id" in str(exc):
                    raise FinanceConflictError("A work entry with the same source_id already exists") from exc
                raise
        return copy.deepcopy(stored)

    def put_work_entries_batch(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not records:
            raise FinanceConflictError("At least one work entry is required")
        now = utc_now_iso()
        stored_records = [copy.deepcopy(record) for record in records]
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for stored in stored_records:
                    stored.setdefault("created_at", now)
                    stored["updated_at"] = now
                    connection.execute(
                        "INSERT INTO work_entries "
                        "(id, source_id, customer_id, contract_id, billing_rule_id, service_date, data, status, billed_invoice_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            stored["id"],
                            stored["source_id"],
                            stored["customer_id"],
                            stored["contract_id"],
                            stored["billing_rule_id"],
                            stored["service_date"],
                            self._encode(stored),
                            stored["status"],
                            stored.get("billed_invoice_id"),
                            stored["created_at"],
                            stored["updated_at"],
                        ),
                    )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "source_id" in str(exc):
                    raise FinanceConflictError("A work entry with the same source_id already exists") from exc
                raise
        return [copy.deepcopy(record) for record in stored_records]

    def list_work_entries(
        self,
        *,
        customer_id: str | None = None,
        contract_id: str | None = None,
        billing_rule_id: str | None = None,
        service_period: str | None = None,
        unbilled_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(str(customer_id))
        if contract_id:
            clauses.append("contract_id = ?")
            params.append(str(contract_id))
        if billing_rule_id:
            clauses.append("billing_rule_id = ?")
            params.append(str(billing_rule_id))
        if service_period:
            clauses.append("substr(service_date, 1, 7) = ?")
            params.append(str(service_period))
        if unbilled_only:
            clauses.append("billed_invoice_id IS NULL")
        query = "SELECT data FROM work_entries WHERE " + " AND ".join(clauses)
        query += " ORDER BY service_date, id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def mark_work_entries_billed(self, entry_ids: list[str], *, invoice_id: str) -> None:
        if not entry_ids:
            raise FinanceConflictError("At least one work entry is required")
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for entry_id in entry_ids:
                row = connection.execute(
                    "SELECT data, billed_invoice_id FROM work_entries WHERE id = ?", (str(entry_id),)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise FinanceNotFoundError(f"Work entry not found: {entry_id}")
                if row["billed_invoice_id"]:
                    connection.rollback()
                    raise FinanceConflictError(f"Work entry is already billed: {entry_id}")
                record = self._decode(row["data"])
                record.update({"status": "BILLED", "billed_invoice_id": invoice_id, "updated_at": now})
                connection.execute(
                    "UPDATE work_entries SET data = ?, status = 'BILLED', billed_invoice_id = ?, updated_at = ? WHERE id = ?",
                    (self._encode(record), invoice_id, now, str(entry_id)),
                )
            connection.commit()

    def put_revenue_entry(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO revenue_entries "
                "(id, customer_id, recognition_date, project_id, department_id, source_type, source_id, data, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["customer_id"],
                    stored["recognition_date"],
                    stored.get("project_id"),
                    stored.get("department_id"),
                    stored["source_type"],
                    stored["source_id"],
                    self._encode(stored),
                    stored["status"],
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
        return copy.deepcopy(stored)

    def find_revenue_entry(self, source_type: str, source_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM revenue_entries WHERE source_type = ? AND source_id = ?",
                (str(source_type), str(source_id)),
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def get_revenue_entry(self, record_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM revenue_entries WHERE id = ?", (str(record_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_revenue_entries(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        customer_id: str | None = None,
        project_id: str | None = None,
        department_id: str | None = None,
        status: str = "ACTIVE",
    ) -> list[dict[str, Any]]:
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if from_date:
            clauses.append("recognition_date >= ?")
            params.append(from_date)
        if to_date:
            clauses.append("recognition_date <= ?")
            params.append(to_date)
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(customer_id)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if department_id:
            clauses.append("department_id = ?")
            params.append(department_id)
        query = (
            "SELECT data FROM revenue_entries WHERE "
            + " AND ".join(clauses)
            + " ORDER BY recognition_date, id"
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def _put_entity(self, table: str, record: dict[str, Any], *, status_key: str) -> dict[str, Any]:
        if table not in {"contracts", "billing_rules"}:
            raise ValueError("unsupported entity table")
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        with self._lock, self._connect() as connection:
            if table == "contracts":
                connection.execute(
                    "INSERT INTO contracts (id, customer_id, data, active_status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (stored["id"], stored["customer_id"], self._encode(stored), stored[status_key], stored["created_at"], stored["updated_at"]),
                )
            else:
                connection.execute(
                    "INSERT INTO billing_rules (id, contract_id, data, active_status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (stored["id"], stored["contract_id"], self._encode(stored), stored[status_key], stored["created_at"], stored["updated_at"]),
                )
        return copy.deepcopy(stored)

    def _get_entity(self, table: str, record_id: str) -> dict[str, Any] | None:
        if table not in {"contracts", "billing_rules"}:
            raise ValueError("unsupported entity table")
        with self._lock, self._connect() as connection:
            row = connection.execute(f"SELECT data FROM {table} WHERE id = ?", (str(record_id),)).fetchone()
        return self._decode(row["data"]) if row else None

    def _list_entities(
        self,
        table: str,
        *,
        customer_id: str | None = None,
        contract_id: str | None = None,
        foreign_key: str,
    ) -> list[dict[str, Any]]:
        if table not in {"contracts", "billing_rules"}:
            raise ValueError("unsupported entity table")
        value = customer_id if foreign_key == "customer_id" else contract_id
        query = f"SELECT data FROM {table}"
        params: tuple[Any, ...] = ()
        if value:
            query += f" WHERE {foreign_key} = ?"
            params = (str(value),)
        query += " ORDER BY created_at, id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def _get_master(self, table: str, record_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(f"SELECT data FROM {table} WHERE id = ?", (str(record_id),)).fetchone()
        return self._decode(row["data"]) if row else None

    def create_invoice(self, record: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        stored.setdefault("revision", 0)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO invoices (id, invoice_number, data, document_status, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored.get("invoice_number"),
                    self._encode(stored),
                    stored["document_status"],
                    stored["revision"],
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
            self._insert_revision(connection, stored, actor)
            self._insert_audit(connection, stored["id"], "invoice.draft_create", actor, {})
            connection.commit()
        return copy.deepcopy(stored)

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT data FROM invoices WHERE id = ?", (str(invoice_id),)).fetchone()
        return self._decode(row["data"]) if row else None

    def list_invoices(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT data FROM invoices").fetchall()
        invoices = [self._decode(row["data"]) for row in rows]
        return sorted(invoices, key=lambda invoice: (str(invoice.get("issue_date") or ""), invoice["id"]))

    def find_invoice_by_billing_key(self, billing_key: str) -> dict[str, Any] | None:
        key = str(billing_key or "").strip()
        if not key:
            return None
        for invoice in self.list_invoices():
            if str(invoice.get("billing_key") or "") == key:
                return invoice
        return None

    def transition_invoice(
        self,
        invoice_id: str,
        *,
        expected_status: str,
        updates: dict[str, Any],
        action: str,
        actor: str,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self._fetch_invoice(connection, invoice_id)
            if record["document_status"] != expected_status:
                connection.rollback()
                raise FinanceConflictError(
                    f"Invoice {invoice_id} is {record['document_status']}, expected {expected_status}"
                )
            record.update(copy.deepcopy(updates))
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = utc_now_iso()
            self._update_invoice_row(connection, record)
            self._insert_revision(connection, record, actor)
            self._insert_audit(connection, invoice_id, action, actor, {})
            connection.commit()
        return copy.deepcopy(record)

    def issue_invoice(
        self,
        invoice_id: str,
        *,
        issued_by: str,
        build_document: Callable[[dict[str, Any], str], dict[str, Any]],
    ) -> dict[str, Any]:
        written_document: dict[str, Any] | None = None
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self._fetch_invoice(connection, invoice_id)
            if record["document_status"] != "APPROVED":
                connection.rollback()
                raise FinanceConflictError("Only APPROVED invoices can be issued")
            period = str(record.get("issue_date") or "")[:7].replace("-", "")
            if len(period) != 6 or not period.isdigit():
                connection.rollback()
                raise FinanceConflictError("Issue date cannot produce an invoice number period")
            row = connection.execute(
                "SELECT next_number FROM invoice_sequences WHERE period = ?", (period,)
            ).fetchone()
            sequence = int(row["next_number"]) if row else 1
            invoice_number = f"INV-{period}-{sequence:04d}"
            if row:
                connection.execute(
                    "UPDATE invoice_sequences SET next_number = ? WHERE period = ?",
                    (sequence + 1, period),
                )
            else:
                connection.execute(
                    "INSERT INTO invoice_sequences (period, next_number) VALUES (?, ?)",
                    (period, sequence + 1),
                )
            canonical = copy.deepcopy(record)
            canonical.update(
                {
                    "invoice_number": invoice_number,
                    "document_status": "ISSUED",
                    "issued_at": utc_now_iso(),
                    "issued_by": issued_by,
                }
            )
            try:
                written_document = build_document(canonical, invoice_number)
                canonical["document"] = copy.deepcopy(written_document)
                canonical["revision"] = int(record.get("revision", 0)) + 1
                canonical["updated_at"] = utc_now_iso()
                self._update_invoice_row(connection, canonical)
                self._insert_revision(connection, canonical, issued_by)
                self._insert_document(connection, invoice_id, written_document)
                self._insert_audit(connection, invoice_id, "invoice.issue", issued_by, {})
                connection.commit()
            except BaseException:
                connection.rollback()
                if written_document:
                    for path_key in ("pdf_path", "json_path"):
                        raw_path = written_document.get(path_key)
                        if raw_path:
                            try:
                                Path(str(raw_path)).unlink()
                            except OSError:
                                pass
                raise
        return copy.deepcopy(canonical)

    def list_audit_events(self, invoice_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT action, actor, detail, created_at FROM audit_events WHERE invoice_id = ? ORDER BY id",
                (str(invoice_id),),
            ).fetchall()
        return [
            {
                "action": row["action"],
                "actor": row["actor"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_payments_batch(
        self,
        batch: dict[str, Any],
        payments: list[dict[str, Any]],
        *,
        imported_by: str,
    ) -> list[dict[str, Any]]:
        now = utc_now_iso()
        batch = copy.deepcopy(batch)
        batch.setdefault("created_at", now)
        stored_payments = [copy.deepcopy(payment) for payment in payments]
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO payment_import_batches (id, data, created_at) VALUES (?, ?, ?)",
                    (batch["id"], self._encode(batch), batch["created_at"]),
                )
                for payment in stored_payments:
                    payment.setdefault("created_at", now)
                    payment["updated_at"] = now
                    connection.execute(
                        "INSERT INTO payments (id, bank_transaction_id, data, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            payment["id"],
                            payment["bank_transaction_id"],
                            self._encode(payment),
                            payment["status"],
                            payment["created_at"],
                            payment["updated_at"],
                        ),
                    )
                self._insert_audit(
                    connection,
                    None,
                    "payment.import",
                    imported_by,
                    {"batch_id": batch["id"], "count": len(stored_payments)},
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "bank_transaction_id" in str(exc):
                    raise FinanceConflictError("A bank transaction already exists") from exc
                raise
        return stored_payments

    def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT data FROM payments WHERE id = ?", (str(payment_id),)).fetchone()
        return self._decode(row["data"]) if row else None

    def list_payments(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM payments ORDER BY created_at, id"
            ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def find_payment_by_transaction_id(self, transaction_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM payments WHERE bank_transaction_id = ?",
                (str(transaction_id),),
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_unallocated_payments(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM payments WHERE status <> 'ALLOCATED' ORDER BY created_at, id"
            ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def list_issued_invoices(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM invoices WHERE document_status = 'ISSUED' ORDER BY invoice_number"
            ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def create_delivery(self, record: dict[str, Any], *, actor: str) -> dict[str, Any]:
        now = utc_now_iso()
        stored = copy.deepcopy(record)
        stored.setdefault("created_at", now)
        stored["updated_at"] = now
        stored.setdefault("attempt_count", 0)
        stored.setdefault("max_attempts", 3)
        stored.setdefault("next_attempt_at", None)
        stored.setdefault("last_error", None)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            invoice = self._fetch_invoice(connection, stored["invoice_id"])
            if invoice["document_status"] != "ISSUED":
                connection.rollback()
                raise FinanceConflictError("Only ISSUED invoices can be queued for delivery")
            invoice["delivery_status"] = stored["status"]
            invoice["revision"] = int(invoice.get("revision", 0)) + 1
            invoice["updated_at"] = now
            self._update_invoice_row(connection, invoice)
            connection.execute(
                "INSERT INTO delivery_records (id, invoice_id, status, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stored["id"],
                    stored["invoice_id"],
                    stored["status"],
                    self._encode(stored),
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
            connection.execute(
                "INSERT INTO delivery_outbox "
                "(id, delivery_id, idempotency_key, status, attempts, data, available_at, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"out_{uuid.uuid4().hex}",
                    stored["id"],
                    f"delivery:{stored['id']}",
                    stored["status"],
                    int(stored["attempt_count"]),
                    self._encode(
                        {
                            "delivery_id": stored["id"],
                            "invoice_id": stored["invoice_id"],
                            "method": stored["method"],
                        }
                    ),
                    stored.get("next_attempt_at"),
                    stored.get("last_error"),
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
            self._insert_revision(connection, invoice, actor)
            self._insert_audit(
                connection,
                invoice["id"],
                "delivery.prepare",
                actor,
                {"delivery_id": stored["id"], "method": stored["method"]},
            )
            connection.commit()
        return copy.deepcopy(stored)

    def list_delivery_outbox(self, *, invoice_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT o.id, o.delivery_id, d.invoice_id, o.idempotency_key, o.status, "
            "o.attempts, o.available_at, o.last_error, o.created_at, o.updated_at "
            "FROM delivery_outbox o JOIN delivery_records d ON d.id = o.delivery_id"
        )
        params: tuple[Any, ...] = ()
        if invoice_id:
            query += " WHERE d.invoice_id = ?"
            params = (str(invoice_id),)
        query += " ORDER BY o.created_at, o.id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM delivery_records WHERE id = ?", (str(delivery_id),)
            ).fetchone()
        return self._decode(row["data"]) if row else None

    def list_deliveries(self, invoice_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            if invoice_id:
                rows = connection.execute(
                    "SELECT data FROM delivery_records WHERE invoice_id = ? ORDER BY created_at, id",
                    (str(invoice_id),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT data FROM delivery_records ORDER BY created_at, id"
                ).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def update_delivery(
        self,
        delivery_id: str,
        *,
        expected_status: str | None,
        updates: dict[str, Any],
        action: str,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM delivery_records WHERE id = ?", (str(delivery_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Delivery not found: {delivery_id}")
            record = self._decode(row["data"])
            if expected_status and record.get("status") != expected_status:
                connection.rollback()
                raise FinanceConflictError(
                    f"Delivery {delivery_id} is {record.get('status')}, expected {expected_status}"
                )
            prior_status = record.get("status")
            record.update(copy.deepcopy(updates))
            if record.get("status") == "SENDING" and prior_status != "SENDING":
                record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
            if record.get("status") in {"SENT", "DELIVERED"}:
                record["next_attempt_at"] = None
            record["updated_at"] = now
            status = str(record.get("status") or "")
            connection.execute(
                "UPDATE delivery_records SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                (status, self._encode(record), now, str(delivery_id)),
            )
            connection.execute(
                "INSERT INTO delivery_outbox "
                "(id, delivery_id, idempotency_key, status, attempts, data, available_at, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(delivery_id) DO UPDATE SET status = excluded.status, attempts = excluded.attempts, "
                "available_at = excluded.available_at, last_error = excluded.last_error, updated_at = excluded.updated_at",
                (
                    f"out_{uuid.uuid4().hex}",
                    record["id"],
                    f"delivery:{record['id']}",
                    status,
                    int(record.get("attempt_count", 0)),
                    self._encode(
                        {
                            "delivery_id": record["id"],
                            "invoice_id": record["invoice_id"],
                            "method": record.get("method"),
                        }
                    ),
                    record.get("next_attempt_at"),
                    record.get("last_error"),
                    record.get("created_at", now),
                    now,
                ),
            )
            invoice = self._fetch_invoice(connection, record["invoice_id"])
            if status in {"QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED"}:
                invoice["delivery_status"] = status
                invoice["revision"] = int(invoice.get("revision", 0)) + 1
                invoice["updated_at"] = now
                self._update_invoice_row(connection, invoice)
                self._insert_revision(connection, invoice, actor)
            self._insert_audit(
                connection,
                invoice["id"],
                action,
                actor,
                {"delivery_id": delivery_id, "status": status},
            )
            connection.commit()
        return copy.deepcopy(record)

    def retry_delivery(self, delivery_id: str, *, actor: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM delivery_records WHERE id = ?", (str(delivery_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Delivery not found: {delivery_id}")
            record = self._decode(row["data"])
            if record.get("status") != "FAILED":
                connection.rollback()
                raise FinanceConflictError("Only FAILED deliveries can be retried")
            attempts = int(record.get("attempt_count", 0))
            maximum = int(record.get("max_attempts", 3))
            if attempts >= maximum:
                connection.rollback()
                raise FinanceConflictError("Delivery retry limit has been reached")
            record.update({"status": "QUEUED", "next_attempt_at": None, "updated_at": now})
            connection.execute(
                "UPDATE delivery_records SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                ("QUEUED", self._encode(record), now, str(delivery_id)),
            )
            invoice = self._fetch_invoice(connection, record["invoice_id"])
            invoice["delivery_status"] = "QUEUED"
            invoice["revision"] = int(invoice.get("revision", 0)) + 1
            invoice["updated_at"] = now
            self._update_invoice_row(connection, invoice)
            connection.execute(
                "INSERT OR IGNORE INTO delivery_outbox "
                "(id, delivery_id, idempotency_key, status, attempts, data, available_at, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, 'QUEUED', ?, ?, NULL, ?, ?, ?)",
                (
                    f"out_{uuid.uuid4().hex}",
                    record["id"],
                    f"delivery:{record['id']}",
                    attempts,
                    self._encode(
                        {
                            "delivery_id": record["id"],
                            "invoice_id": record["invoice_id"],
                            "method": record.get("method"),
                        }
                    ),
                    record.get("last_error"),
                    record.get("created_at", now),
                    now,
                ),
            )
            connection.execute(
                "UPDATE delivery_outbox SET status = 'QUEUED', attempts = ?, available_at = NULL, "
                "updated_at = ? WHERE delivery_id = ?",
                (attempts, now, str(delivery_id)),
            )
            self._insert_revision(connection, invoice, actor)
            self._insert_audit(
                connection,
                invoice["id"],
                "delivery.retry",
                actor,
                {"delivery_id": delivery_id, "attempt_count": attempts},
            )
            connection.commit()
        return copy.deepcopy(record)

    def payment_allocations(self, payment_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payment_id, invoice_id, allocated_amount, allocation_method, matched_reason, approved_by, allocated_at "
                "FROM payment_allocations WHERE payment_id = ? ORDER BY allocated_at, id",
                (str(payment_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def invoice_allocations(self, invoice_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payment_id, invoice_id, allocated_amount, allocation_method, matched_reason, approved_by, allocated_at "
                "FROM payment_allocations WHERE invoice_id = ? ORDER BY allocated_at, id",
                (str(invoice_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def allocate_payment(
        self,
        payment_id: str,
        allocations: list[dict[str, Any]],
        *,
        approved_by: str,
    ) -> dict[str, Any]:
        if not allocations:
            raise FinanceConflictError("At least one payment allocation is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            payment_row = connection.execute(
                "SELECT data FROM payments WHERE id = ?", (str(payment_id),)
            ).fetchone()
            if payment_row is None:
                connection.rollback()
                raise FinanceNotFoundError(f"Payment not found: {payment_id}")
            payment = self._decode(payment_row["data"])
            invoice_ids = [str(item.get("invoice_id")) for item in allocations]
            if len(invoice_ids) != len(set(invoice_ids)):
                connection.rollback()
                raise FinanceConflictError("Each invoice may appear only once per allocation")
            payment_amount = Decimal(str(payment["amount"]))
            existing_payment_allocated = sum(
                (Decimal(str(row["allocated_amount"])) for row in connection.execute(
                    "SELECT allocated_amount FROM payment_allocations WHERE payment_id = ?",
                    (str(payment_id),),
                ).fetchall()),
                Decimal("0"),
            )
            requested_total = Decimal("0")
            invoice_records: list[tuple[dict[str, Any], Decimal, dict[str, Any]]] = []
            for item in allocations:
                try:
                    amount = Decimal(str(item.get("amount")))
                except Exception as exc:
                    connection.rollback()
                    raise FinanceConflictError("Allocation amount must be numeric") from exc
                if not amount.is_finite() or amount <= 0:
                    connection.rollback()
                    raise FinanceConflictError("Allocation amount must be greater than zero")
                invoice = self._fetch_invoice(connection, str(item.get("invoice_id")))
                if invoice["document_status"] != "ISSUED":
                    connection.rollback()
                    raise FinanceConflictError("Only ISSUED invoices can receive allocations")
                if invoice["currency"] != payment["currency"]:
                    connection.rollback()
                    raise FinanceConflictError("Payment and invoice currencies must match")
                existing_allocation = connection.execute(
                    "SELECT 1 FROM payment_allocations WHERE payment_id = ? AND invoice_id = ?",
                    (str(payment_id), invoice["id"]),
                ).fetchone()
                if existing_allocation is not None:
                    connection.rollback()
                    raise FinanceConflictError(
                        "A payment can be allocated to the same invoice only once"
                    )
                prior_invoice_allocated = sum(
                    (
                        Decimal(str(row["allocated_amount"]))
                        for row in connection.execute(
                            "SELECT allocated_amount FROM payment_allocations WHERE invoice_id = ?",
                            (invoice["id"],),
                        ).fetchall()
                    ),
                    Decimal("0"),
                )
                requested_total += amount
                invoice_records.append((invoice, prior_invoice_allocated, {**item, "amount": amount}))
            if existing_payment_allocated + requested_total > payment_amount:
                connection.rollback()
                raise FinanceConflictError("Allocation exceeds the payment amount")

            now = utc_now_iso()
            for invoice, prior_invoice_allocated, item in invoice_records:
                amount = item["amount"]
                total = Decimal(str(invoice["totals"]["total"]))
                allocated = prior_invoice_allocated + amount
                if allocated == 0:
                    settlement = "UNPAID"
                elif allocated < total:
                    settlement = "PARTIALLY_PAID"
                elif allocated == total:
                    settlement = "PAID"
                else:
                    settlement = "OVERPAID"
                invoice["settlement_status"] = settlement
                invoice["revision"] = int(invoice.get("revision", 0)) + 1
                invoice["updated_at"] = now
                self._update_invoice_row(connection, invoice)
                connection.execute(
                    "INSERT INTO payment_allocations (id, payment_id, invoice_id, allocated_amount, allocation_method, matched_reason, approved_by, allocated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"alloc_{uuid.uuid4().hex}",
                        payment_id,
                        invoice["id"],
                        format(amount, "f"),
                        str(item.get("allocation_method") or "manual"),
                        item.get("matched_reason"),
                        approved_by,
                        now,
                    ),
                )
                self._insert_revision(connection, invoice, approved_by)
                self._insert_audit(
                    connection,
                    invoice["id"],
                    "payment.allocate",
                    approved_by,
                    {"payment_id": payment_id, "allocated_amount": format(amount, "f")},
                )
            new_total = existing_payment_allocated + requested_total
            payment["status"] = "ALLOCATED" if new_total == payment_amount else "PARTIALLY_ALLOCATED"
            payment["updated_at"] = now
            connection.execute(
                "UPDATE payments SET data = ?, status = ?, updated_at = ? WHERE id = ?",
                (self._encode(payment), payment["status"], now, payment_id),
            )
            connection.commit()
        return {
            "payment_id": payment_id,
            "payment_status": payment["status"],
            "allocations": self.payment_allocations(payment_id),
        }

    @staticmethod
    def _fetch_invoice(connection: sqlite3.Connection, invoice_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT data FROM invoices WHERE id = ?", (str(invoice_id),)).fetchone()
        if row is None:
            raise FinanceNotFoundError(f"Invoice not found: {invoice_id}")
        return json.loads(row["data"])

    @staticmethod
    def _update_invoice_row(connection: sqlite3.Connection, record: dict[str, Any]) -> None:
        connection.execute(
            "UPDATE invoices SET invoice_number = ?, data = ?, document_status = ?, revision = ?, updated_at = ? WHERE id = ?",
            (
                record.get("invoice_number"),
                json.dumps(record, ensure_ascii=False, sort_keys=True),
                record["document_status"],
                int(record["revision"]),
                record["updated_at"],
                record["id"],
            ),
        )

    @staticmethod
    def _insert_revision(connection: sqlite3.Connection, record: dict[str, Any], actor: str | None) -> None:
        connection.execute(
            "INSERT INTO invoice_revisions (invoice_id, revision, data, actor, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                record["id"],
                int(record["revision"]),
                json.dumps(record, ensure_ascii=False, sort_keys=True),
                actor,
                utc_now_iso(),
            ),
        )

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        invoice_id: str | None,
        action: str,
        actor: str | None,
        detail: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events (invoice_id, action, actor, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (invoice_id, action, actor, json.dumps(detail, ensure_ascii=False), utc_now_iso()),
        )

    @staticmethod
    def _insert_document(connection: sqlite3.Connection, invoice_id: str, document: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO documents (id, invoice_id, kind, path, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"doc_{uuid.uuid4().hex}",
                invoice_id,
                "issued_invoice",
                document["pdf_path"],
                document["pdf_sha256"],
                utc_now_iso(),
            ),
        )
