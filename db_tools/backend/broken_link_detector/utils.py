"""Utility helpers for the Broken Link Detector.

No DB access — pure logic so it stays unit-testable.
"""

import frappe

# Default severity classification (spec §7). Configurable via ``severity_map``.
DEFAULT_CRITICAL = {
    "User",
    "Company",
    "Account",
    "Warehouse",
    "Cost Center",
    "Fiscal Year",
    "Department",
    "Branch",
    "DefaultValue",
    "Series",
}

DEFAULT_WARNING = {
    "Customer",
    "Supplier",
    "Employee",
    "Item",
    "Project",
    "Sales Order",
    "Purchase Order",
    "Address",
    "Contact",
}


def is_empty(value) -> bool:
    """Return True for NULL, empty strings and whitespace-only values."""
    return value is None or (isinstance(value, str) and not value.strip())


def classify_severity(target_doctype: str, severity_map: dict | None = None) -> str:
    """Classify a finding's severity based on the target DocType.

    ``severity_map`` may map a DocType to ``critical``/``warning``/``info`` and
    overrides the defaults.
    """
    mapping = severity_map or {}
    key = target_doctype or ""

    if key in mapping:
        return mapping[key]
    if key in DEFAULT_CRITICAL:
        return "critical"
    if key in DEFAULT_WARNING:
        return "warning"
    return "info"


def quote_table(doctype: str) -> str:
    """Backtick-quote a table name (supports DocType names with spaces)."""
    return f"`tab{doctype}`"


def quote_column(fieldname: str) -> str:
    """Backtick-quote a column name."""
    return f"`{fieldname}`"


def as_bool(value) -> bool:
    """Coerce query-string bools (from whitelisted APIs) into Python bools."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")
