"""Discord skills for the canonical Finance Core integration.

Invoice state, authorization, rendering, delivery, and payment data are owned
by the Finance Core MCP server.  This plugin only publishes the natural-
language skill instructions that teach Hermes how to use that server.
"""

from __future__ import annotations

from pathlib import Path


def register(ctx) -> None:
    """Register Finance Core skills without exposing a second invoice tool."""

    skill_paths = (
        (
            "skill-fin-discord-finance-workflow",
            "Canonical Discord natural-language workflow for Finance Core invoices, calendar billing, payments, and delivery.",
        ),
        (
            "skill-fin-customer-management",
            "Manage customer master data, contacts, lifecycle, matching, and portal access through Finance Core.",
        ),
        (
            "skill-fin-invoice-generation",
            "Create and issue invoices through the independent Finance Core MCP.",
        ),
        (
            "skill-fin-payment-status-check",
            "Import bank receipts and reconcile invoice payments through Finance Core.",
        ),
        (
            "skill-fin-revenue-tracking",
            "Summarize revenue, receivables, and invoice reminders through Finance Core.",
        ),
        (
            "skill-fin-spreadsheet-integration",
            "Exchange redacted payment and accounting CSV data with Finance Core.",
        ),
    )

    skills_root = Path(__file__).with_name("skills")
    for skill_name, description in skill_paths:
        skill_path = skills_root / skill_name / "SKILL.md"
        if skill_path.exists():
            ctx.register_skill(name=skill_name, path=skill_path, description=description)
