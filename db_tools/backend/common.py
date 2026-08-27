"""Shared helpers for every tool under ``db_tools.backend``.

Keeps the tools consistent: the same permission gate, the same coercion of
query-string arguments, and the same ``json``/``csv``/``markdown`` renderers so
every dashboard can offer the same download buttons.
"""

import csv
import io

import frappe

# Roles allowed to run the tools. These endpoints expose schema and row level
# information about the site, so they are never open to guests.
ALLOWED_ROLES = {"System Manager", "Administrator"}

# Framework columns that are populated on every row by definition — noise for
# any tool that looks at how columns are filled in.
STANDARD_COLUMNS_ALWAYS_SET = {
    "name",
    "owner",
    "creation",
    "modified",
    "modified_by",
    "docstatus",
    "idx",
}

# Frappe/ERPNext tables that are not backed by a DocType but are still expected.
KNOWN_SYSTEM_TABLES = {
    "__Auth",
    "__global_search",
    "__UserSettings",
    "_asset_repository",
}


def guard():
    """Raise unless the current user may run a database tool."""
    if frappe.session.user == "Administrator":
        return

    if frappe.session.user in ("Guest", None, ""):
        raise frappe.PermissionError("Please log in to use the database tools.")

    if not (set(frappe.get_roles()) & ALLOWED_ROLES):
        raise frappe.PermissionError("You need the System Manager role to use the database tools.")


def guard_page(context=None):
    """Website-page guard for every page under /db_tools.

    Guests are sent to the login screen (and back here afterwards); logged-in
    users without the System Manager role get a 403.
    """
    if frappe.session.user in ("Guest", None, ""):
        path = getattr(frappe.request, "full_path", None) or getattr(frappe.request, "path", "/db_tools")
        frappe.local.flags.redirect_location = "/login?redirect-to=" + frappe.utils.quoted(path.rstrip("?"))
        raise frappe.Redirect

    guard()

    if context is not None:
        from db_tools.backend import AUTHOR

        context.no_cache = 1
        # Every page renders the attribution from here, so it stays in one place.
        context.author = AUTHOR

    return context


def as_bool(value) -> bool:
    """Coerce query-string bools (from whitelisted APIs) into Python bools."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def as_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_list(value) -> list:
    """Accept a comma separated string, a JSON array or a list."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]

    text = str(value).strip()
    if text.startswith("["):
        try:
            return as_list(frappe.parse_json(text))
        except Exception:
            pass
    return [part.strip() for part in text.split(",") if part.strip()]


def db_name() -> str:
    return frappe.conf.db_name


def is_mariadb() -> bool:
    return (frappe.conf.db_type or "mariadb") in ("mariadb", "mysql")


def require_mariadb():
    if not is_mariadb():
        frappe.throw(
            "This tool currently supports MariaDB/MySQL only "
            f"(this site runs on {frappe.conf.db_type})."
        )


def fmt_bytes(num) -> str:
    """Human readable byte size."""
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def table_of(doctype: str) -> str:
    return f"tab{doctype}"


def doctype_of(table: str) -> str:
    return table[3:] if table.startswith("tab") else table


def quote(identifier: str) -> str:
    """Backtick-quote an identifier, escaping embedded backticks."""
    return "`" + str(identifier).replace("`", "``") + "`"


def existing_tables() -> set:
    """All table names in the current database."""
    require_mariadb()
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """,
        (db_name(),),
        as_dict=True,
    )
    return {r.name for r in rows}


def table_columns(table: str) -> set:
    rows = frappe.db.sql(
        """
        SELECT COLUMN_NAME AS name
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (db_name(), table),
        as_dict=True,
    )
    return {r.name for r in rows}


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def to_csv(rows: list, columns: list) -> str:
    """Render a list of dicts as CSV. ``columns`` is a list of keys."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(col)) for col in columns])
    return buf.getvalue()


def to_markdown(rows: list, columns: list, title: str = "") -> str:
    out = []
    if title:
        out.append(f"# {title}\n")

    if not rows:
        out.append("_No rows._\n")
        return "\n".join(out)

    out.append("| " + " | ".join(columns) + " |")
    out.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        out.append("| " + " | ".join(_cell(row.get(col)).replace("|", "\\|") for col in columns) + " |")
    out.append("")
    return "\n".join(out)


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render(payload: dict, fmt: str, rows_key: str, columns: list, title: str = "") -> dict:
    """Return a ``{"data": ..., "format": ...}`` envelope for downloads."""
    rows = payload.get(rows_key) or []

    if fmt == "csv":
        return {"data": to_csv(rows, columns), "format": "csv"}
    if fmt in ("markdown", "md"):
        return {"data": to_markdown(rows, columns, title), "format": "markdown"}

    return {"data": frappe.as_json(payload, indent=2), "format": "json"}


def respond(payload: dict, fmt: str, rows_key: str, columns: list, title: str = ""):
    """Either return the structured payload or a rendered download envelope."""
    if fmt and fmt != "json":
        return render(payload, fmt, rows_key, columns, title)
    return payload
