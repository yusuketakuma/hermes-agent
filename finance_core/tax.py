"""Deterministic invoice tax and total calculations."""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Any

from finance_core.errors import FinanceValidationError


TAX_CATEGORIES = {
    "STANDARD_10": Decimal("10"),
    "REDUCED_8": Decimal("8"),
    "EXEMPT": Decimal("0"),
    "OUT_OF_SCOPE": Decimal("0"),
}
_ROUNDING = {
    "DOWN": ROUND_DOWN,
    "HALF_UP": ROUND_HALF_UP,
    "UP": ROUND_UP,
}


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field} must be a valid decimal") from exc
    if not number.is_finite():
        raise FinanceValidationError(f"{field} must be finite")
    return number


def _quantum(currency: str) -> Decimal:
    return Decimal("1") if currency.upper() == "JPY" else Decimal("0.01")


def _amount(value: Decimal, quantum: Decimal, rounding=ROUND_HALF_UP) -> str:
    return format(value.quantize(quantum, rounding=rounding), "f")


def calculate_totals(
    lines: list[dict[str, Any]],
    *,
    currency: str,
    rounding: str = "HALF_UP",
) -> dict[str, Any]:
    """Calculate totals with one tax rounding operation per tax category.

    Tax category is mandatory and is never inferred from a description. The
    initial Japanese policy maps STANDARD_10 and REDUCED_8 to their statutory
    rates; EXEMPT and OUT_OF_SCOPE must use a zero tax rate.
    """

    normalized_currency = str(currency or "").upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise FinanceValidationError("currency must be a three-letter code")
    try:
        rounding_mode = _ROUNDING[str(rounding).upper()]
    except KeyError as exc:
        raise FinanceValidationError("rounding must be DOWN, HALF_UP, or UP") from exc
    if not isinstance(lines, list) or not lines:
        raise FinanceValidationError("lines must contain at least one line")

    quantum = _quantum(normalized_currency)
    subtotal = Decimal("0")
    category_bases: OrderedDict[str, Decimal] = OrderedDict()
    normalized_lines: list[dict[str, Any]] = []
    for index, raw in enumerate(lines, start=1):
        if not isinstance(raw, dict):
            raise FinanceValidationError(f"lines[{index}] must be an object")
        category = str(raw.get("tax_category") or "").strip().upper()
        if category not in TAX_CATEGORIES:
            raise FinanceValidationError(
                f"lines[{index}].tax_category must be one of {', '.join(TAX_CATEGORIES)}"
            )
        quantity = _decimal(raw.get("quantity"), field=f"lines[{index}].quantity")
        unit_price = _decimal(raw.get("unit_price"), field=f"lines[{index}].unit_price")
        tax_rate = _decimal(raw.get("tax_rate"), field=f"lines[{index}].tax_rate")
        if quantity <= 0:
            raise FinanceValidationError(f"lines[{index}].quantity must be greater than zero")
        if unit_price < 0:
            raise FinanceValidationError(f"lines[{index}].unit_price cannot be negative")
        if tax_rate != TAX_CATEGORIES[category]:
            raise FinanceValidationError(
                f"lines[{index}].tax_rate does not match tax_category {category}"
            )
        description = str(raw.get("description") or "").strip()
        if not description or len(description) > 1000:
            raise FinanceValidationError(f"lines[{index}].description is required and bounded")
        line_subtotal = quantity * unit_price
        subtotal += line_subtotal
        category_bases[category] = category_bases.get(category, Decimal("0")) + line_subtotal
        normalized_lines.append(
            {
                "description": description,
                "quantity": format(quantity, "f"),
                "unit": str(raw.get("unit") or "unit")[:100],
                "unit_price": _amount(unit_price, quantum),
                "tax_category": category,
                "tax_rate": _amount(tax_rate, Decimal("1")),
                "service_date_or_period": str(raw.get("service_date_or_period") or raw.get("service_period") or ""),
                "source_type": str(raw.get("source_type") or "manual"),
                "source_id": raw.get("source_id"),
                "billing_rule_version_id": raw.get("billing_rule_version_id"),
                "calculation_expression": raw.get("calculation_expression"),
                "generated_by": str(raw.get("generated_by") or "manual"),
                "line_subtotal": _amount(line_subtotal, quantum),
            }
        )

    breakdown: list[dict[str, str]] = []
    tax_total = Decimal("0")
    for category, base in category_bases.items():
        rate = TAX_CATEGORIES[category]
        tax_amount = (base * rate / Decimal("100")).quantize(quantum, rounding=rounding_mode)
        tax_total += tax_amount
        breakdown.append(
            {
                "tax_category": category,
                "tax_rate": _amount(rate, Decimal("1")),
                "taxable_amount": _amount(base, quantum),
                "tax_amount": _amount(tax_amount, quantum),
            }
        )
    return {
        "subtotal": _amount(subtotal, quantum),
        "tax": _amount(tax_total, quantum),
        "total": _amount(subtotal + tax_total, quantum),
        "tax_breakdown": breakdown,
        "lines": normalized_lines,
        "rounding": str(rounding).upper(),
    }


def calculate_withholding_tax(
    base: Decimal | str,
    rate: Decimal | str,
    *,
    currency: str,
    rounding: str = "HALF_UP",
) -> str:
    """Calculate explicitly configured withholding tax.

    The caller must provide both the base and rate.  Finance Core never
    infers withholding from a customer type or line description.
    """

    normalized_base = _decimal(base, field="withholding_base")
    normalized_rate = _decimal(rate, field="withholding_tax_rate")
    if normalized_base < 0:
        raise FinanceValidationError("withholding_base cannot be negative")
    if normalized_rate < 0 or normalized_rate > 100:
        raise FinanceValidationError("withholding_tax_rate must be between 0 and 100")
    normalized_currency = str(currency or "").upper()
    quantum = _quantum(normalized_currency)
    try:
        rounding_mode = _ROUNDING[str(rounding).upper()]
    except KeyError as exc:
        raise FinanceValidationError("rounding must be DOWN, HALF_UP, or UP") from exc
    amount = normalized_base * normalized_rate / Decimal("100")
    return _amount(amount, quantum, rounding=rounding_mode)
