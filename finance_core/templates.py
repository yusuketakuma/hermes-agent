"""Immutable, data-only invoice template versions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from finance_core.errors import FinanceNotFoundError, FinanceValidationError


class TemplateRegistry:
    """Load approved layout files without storing customer data in templates."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else Path(__file__).with_name("templates")

    def _directory(self, template_id: str, version: str) -> Path:
        directory = self.root / template_id / version
        if not directory.is_dir():
            raise FinanceNotFoundError(f"Template version not found: {template_id}@{version}")
        return directory

    def get(self, template_id: str, version: str) -> dict[str, Any]:
        directory = self._directory(template_id, version)
        manifest_path = directory / "template.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FinanceValidationError(f"Template metadata is invalid: {template_id}@{version}") from exc
        if manifest.get("template_id") != template_id or manifest.get("semantic_version") != version:
            raise FinanceValidationError("Template metadata identity does not match its path")
        manifest = dict(manifest)
        manifest["layout_files_sha256"] = {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("invoice.html", "invoice.css")
            if (directory / name).is_file()
        }
        manifest["directory"] = str(directory)
        return manifest

    def list_versions(self, template_id: str | None = None) -> list[dict[str, Any]]:
        roots = [self.root / template_id] if template_id else sorted(self.root.iterdir())
        versions: list[dict[str, Any]] = []
        for template_root in roots:
            if not template_root.is_dir():
                continue
            for version_root in sorted(template_root.iterdir()):
                if version_root.is_dir() and (version_root / "template.json").exists():
                    versions.append(self.get(template_root.name, version_root.name))
        return versions

    def render_html(self, invoice: dict[str, Any], *, draft: bool) -> str:
        template_id = str(invoice.get("template_id") or "")
        version = str(invoice.get("template_version") or "")
        metadata = self.get(template_id, version)
        directory = Path(metadata["directory"])
        environment = Environment(
            loader=FileSystemLoader(str(directory)),
            undefined=StrictUndefined,
            autoescape=select_autoescape(("html", "xml")),
        )
        css = (directory / "invoice.css").read_text(encoding="utf-8")
        return environment.get_template("invoice.html").render(
            invoice=invoice,
            issuer=invoice.get("issuer") or {},
            customer=invoice.get("customer") or {},
            draft=draft,
            stylesheet=css,
        )
