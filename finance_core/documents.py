"""Private immutable document storage for issued Finance Core artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from finance_core.errors import FinanceConflictError


class DocumentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "documents"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @staticmethod
    def _write_private(path: Path, payload: bytes, *, overwrite: bool = False) -> None:
        if path.exists() and not overwrite:
            raise FinanceConflictError(f"Document already exists: {path.name}")
        fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".finance-", suffix=".tmp")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def write_preview(self, invoice_id: str, pdf_bytes: bytes) -> dict[str, Any]:
        path = self.root / "previews" / f"draft_{invoice_id}.pdf"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = bytes(pdf_bytes)
        self._write_private(path, payload, overwrite=True)
        return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}

    def write_issued(
        self,
        invoice_number: str,
        canonical_invoice: dict[str, Any],
        pdf_bytes: bytes,
    ) -> dict[str, Any]:
        safe_number = str(invoice_number)
        if not safe_number or "/" in safe_number or "\\" in safe_number:
            raise FinanceConflictError("Invalid invoice number for document storage")
        pdf_path = self.root / f"invoice_{safe_number}.pdf"
        json_path = self.root / f"invoice_{safe_number}.json"
        json_payload = json.dumps(
            canonical_invoice,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        pdf_payload = bytes(pdf_bytes)
        written: list[Path] = []
        try:
            self._write_private(json_path, json_payload)
            written.append(json_path)
            self._write_private(pdf_path, pdf_payload)
            written.append(pdf_path)
        except BaseException:
            for path in written:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return {
            "pdf_path": str(pdf_path),
            "json_path": str(json_path),
            "pdf_sha256": hashlib.sha256(pdf_payload).hexdigest(),
            "json_sha256": hashlib.sha256(json_payload).hexdigest(),
        }

    @staticmethod
    def cleanup(document: dict[str, Any] | None) -> None:
        if not document:
            return
        for key in ("pdf_path", "json_path", "path"):
            raw_path = document.get(key)
            if raw_path:
                try:
                    Path(str(raw_path)).unlink()
                except OSError:
                    pass
