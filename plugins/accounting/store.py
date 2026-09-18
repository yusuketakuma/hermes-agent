"""Profile-scoped durable storage for accounting records."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DuplicateInvoiceNumber(ValueError):
    """Raised when an invoice number is already reserved."""


class AccountingStore:
    """Small SQLite store with transactional invoice state transitions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.documents_dir = self.path.parent / "invoices"
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.documents_dir, 0o700)
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
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_invoice_id_idx
                    ON events(invoice_id, id);
                CREATE TABLE IF NOT EXISTS invoice_numbers (
                    invoice_number TEXT PRIMARY KEY,
                    invoice_id TEXT NOT NULL UNIQUE
                );
                """
            )
            # Backfill reservations for databases created before the unique
            # reservation table existed. INSERT OR IGNORE keeps startup
            # recoverable even if an old database already contains duplicates;
            # all newly-created invoices remain strictly unique.
            connection.execute(
                "INSERT OR IGNORE INTO invoice_numbers (invoice_number, invoice_id) "
                "SELECT json_extract(data, '$.invoice_number'), id FROM invoices "
                "WHERE COALESCE(json_extract(data, '$.invoice_number'), '') <> ''"
            )

    @staticmethod
    def _decode(data: str) -> dict[str, Any]:
        record = json.loads(data)
        record.setdefault("revision", 0)
        return record

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        invoice_id: str,
        action: str,
        actor: str | None,
        detail: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            "INSERT INTO events (invoice_id, action, actor, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(invoice_id),
                action,
                actor,
                json.dumps(detail or {}, ensure_ascii=False),
                utc_now_iso(),
            ),
        )

    def create(
        self,
        invoice: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        record = copy.deepcopy(invoice)
        timestamp = utc_now_iso()
        record.setdefault("created_at", timestamp)
        record.setdefault("updated_at", timestamp)
        record.setdefault("revision", 0)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO invoices (id, data) VALUES (?, ?)",
                    (record["id"], json.dumps(record, ensure_ascii=False)),
                )
                invoice_number = str(record.get("invoice_number") or "").strip()
                if invoice_number:
                    try:
                        connection.execute(
                            "INSERT INTO invoice_numbers (invoice_number, invoice_id) "
                            "VALUES (?, ?)",
                            (invoice_number, str(record["id"])),
                        )
                    except sqlite3.IntegrityError as exc:
                        if "invoice_numbers.invoice_number" in str(exc):
                            raise DuplicateInvoiceNumber(
                                f"Invoice number already exists: {invoice_number}"
                            ) from exc
                        raise
                self._insert_event(
                    connection,
                    record["id"],
                    "create_invoice",
                    actor,
                    {"invoice_number": invoice_number or None},
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return copy.deepcopy(record)

    def get(self, invoice_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM invoices WHERE id = ?", (str(invoice_id),)
            ).fetchone()
        if row is None:
            return None
        return self._decode(row["data"])

    def find_by_invoice_number(self, invoice_number: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM invoices "
                "WHERE json_extract(data, '$.invoice_number') = ? LIMIT 1",
                (str(invoice_number),),
            ).fetchone()
        if row is None:
            return None
        return self._decode(row["data"])

    def list(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT data FROM invoices"
        params: list[Any] = []
        if statuses:
            values = list(statuses)
            placeholders = ",".join("?" for _ in values)
            query += f" WHERE json_extract(data, '$.status') IN ({placeholders})"
            params.extend(values)
        query += " ORDER BY json_extract(data, '$.due_date'), json_extract(data, '$.invoice_number')"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row["data"]) for row in rows]

    def update(
        self,
        invoice_id: str,
        updates: dict[str, Any],
        *,
        action: str | None = None,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM invoices WHERE id = ?", (str(invoice_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            record = self._decode(row["data"])
            record.update(copy.deepcopy(updates))
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = utc_now_iso()
            connection.execute(
                "UPDATE invoices SET data = ? WHERE id = ?",
                (json.dumps(record, ensure_ascii=False), str(invoice_id)),
            )
            if action:
                self._insert_event(connection, invoice_id, action, actor, detail)
            connection.commit()
        return copy.deepcopy(record)

    def update_if_status(
        self,
        invoice_id: str,
        expected_status: str,
        updates: dict[str, Any],
        *,
        action: str | None = None,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update only when the current status still equals the expected value."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM invoices WHERE id = ?", (str(invoice_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            record = self._decode(row["data"])
            if record.get("status") != expected_status:
                connection.rollback()
                return None
            record.update(copy.deepcopy(updates))
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = utc_now_iso()
            connection.execute(
                "UPDATE invoices SET data = ? WHERE id = ?",
                (json.dumps(record, ensure_ascii=False), str(invoice_id)),
            )
            if action:
                self._insert_event(connection, invoice_id, action, actor, detail)
            connection.commit()
        return copy.deepcopy(record)

    def update_if_revision(
        self,
        invoice_id: str,
        expected_revision: int,
        updates: dict[str, Any],
        *,
        action: str | None = None,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update only when the current record has the expected revision."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM invoices WHERE id = ?", (str(invoice_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            record = self._decode(row["data"])
            if int(record.get("revision", 0)) != int(expected_revision):
                connection.rollback()
                return None
            record.update(copy.deepcopy(updates))
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = utc_now_iso()
            connection.execute(
                "UPDATE invoices SET data = ? WHERE id = ?",
                (json.dumps(record, ensure_ascii=False), str(invoice_id)),
            )
            if action:
                self._insert_event(connection, invoice_id, action, actor, detail)
            connection.commit()
        return copy.deepcopy(record)

    def update_many_if_revisions(
        self,
        changes: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Apply several revision-guarded updates atomically."""

        changes = list(changes)
        invoice_ids = [str(change.get("invoice_id")) for change in changes]
        if len(invoice_ids) != len(set(invoice_ids)):
            return None
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            records: list[dict[str, Any]] = []
            for change in changes:
                row = connection.execute(
                    "SELECT data FROM invoices WHERE id = ?",
                    (str(change.get("invoice_id")),),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                record = self._decode(row["data"])
                if int(record.get("revision", 0)) != int(change.get("expected_revision", -1)):
                    connection.rollback()
                    return None
                records.append(record)

            updated: list[dict[str, Any]] = []
            for change, record in zip(changes, records):
                record.update(copy.deepcopy(change.get("updates") or {}))
                record["revision"] = int(record.get("revision", 0)) + 1
                record["updated_at"] = utc_now_iso()
                connection.execute(
                    "UPDATE invoices SET data = ? WHERE id = ?",
                    (json.dumps(record, ensure_ascii=False), str(change["invoice_id"])),
                )
                action = change.get("action")
                if action:
                    self._insert_event(
                        connection,
                        str(change["invoice_id"]),
                        str(action),
                        change.get("actor"),
                        change.get("detail"),
                    )
                updated.append(copy.deepcopy(record))
            connection.commit()
        return updated

    def claim_reminder(
        self,
        invoice_id: str,
        reminder_day: str,
        *,
        lease_seconds: int = 300,
        actor: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim one reminder delivery for a day until its lease expires."""

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM invoices WHERE id = ?", (str(invoice_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            record = self._decode(row["data"])
            if record.get("status") not in {"draft", "pending_approval"}:
                connection.rollback()
                return None
            if record.get("last_reminder_on") == reminder_day:
                connection.rollback()
                return None

            claim_day = record.get("reminder_claim_on")
            claim_at = record.get("reminder_claimed_at")
            if claim_day == reminder_day and claim_at:
                try:
                    age = (now - datetime.fromisoformat(str(claim_at))).total_seconds()
                except (TypeError, ValueError):
                    age = 0
                if age < max(int(lease_seconds), 1):
                    connection.rollback()
                    return None

            record.update(
                {
                    "reminder_claim_on": reminder_day,
                    "reminder_claimed_at": now_iso,
                }
            )
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = now_iso
            connection.execute(
                "UPDATE invoices SET data = ? WHERE id = ?",
                (json.dumps(record, ensure_ascii=False), str(invoice_id)),
            )
            self._insert_event(
                connection,
                invoice_id,
                "claim_reminder",
                actor,
                {"reminder_day": reminder_day},
            )
            connection.commit()
        return copy.deepcopy(record)

    def events(self, invoice_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT action, actor, detail, created_at FROM events "
                "WHERE invoice_id = ? ORDER BY id",
                (str(invoice_id),),
            ).fetchall()
        return [
            {
                "action": row["action"],
                "actor": row["actor"],
                "detail": json.loads(row["detail"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
