"""Estimate inline row size per table and flag tables near the InnoDB limit."""

import time

import frappe

from db_tools.backend.common import (
    as_int,
    db_name,
    doctype_of,
    guard,
    require_mariadb,
    respond,
)

COLUMNS = [
    "table_name",
    "doctype",
    "columns",
    "varchar_columns",
    "text_columns",
    "estimated_row_bytes",
    "pct_of_limit",
    "row_format",
    "severity",
]

# InnoDB's hard ceiling on inline row data.
ROW_SIZE_LIMIT = 65535

# Off-page types: DYNAMIC/COMPRESSED store these as a 20-byte pointer.
OFF_PAGE_TYPES = {"text", "mediumtext", "longtext", "blob", "mediumblob", "longblob", "json"}
OFF_PAGE_POINTER = 20

# Fixed inline widths for the non-string types Frappe uses.
FIXED_WIDTHS = {
    "tinyint": 1, "smallint": 2, "mediumint": 3, "int": 4, "bigint": 8,
    "float": 4, "double": 8, "date": 3, "datetime": 8, "timestamp": 4,
    "time": 3, "year": 1, "tinytext": 255,
}


def column_inline_bytes(col, dynamic_row_format: bool) -> int:
    """Inline bytes this column contributes to a row."""
    dtype = col.dtype

    if dtype in OFF_PAGE_TYPES:
        # COMPACT/REDUNDANT keep the first 768 bytes of the value in the row.
        return OFF_PAGE_POINTER if dynamic_row_format else 768

    if dtype in ("varchar", "char"):
        # CHARACTER_OCTET_LENGTH already accounts for the charset's bytes/char.
        return int(col.octet_len or 0) + 2

    if dtype == "decimal":
        precision = int(col.nprec or 10)
        return (precision // 9) * 4 + 4

    return FIXED_WIDTHS.get(dtype, 8)


def audit_row_sizes(warn_pct: int = 60, max_columns: int = 200):
    require_mariadb()
    started = time.monotonic()

    formats = {
        r.name: r
        for r in frappe.db.sql(
            """
            SELECT TABLE_NAME AS name, ROW_FORMAT AS row_format, ENGINE AS engine,
                   TABLE_ROWS AS n_rows
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
            """,
            (db_name(),),
            as_dict=True,
        )
    }

    cols = frappe.db.sql(
        """
        SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col, DATA_TYPE AS dtype,
               CHARACTER_OCTET_LENGTH AS octet_len, NUMERIC_PRECISION AS nprec
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        """,
        (db_name(),),
        as_dict=True,
    )

    per_table = {}
    for c in cols:
        per_table.setdefault(c.tbl, []).append(c)

    rows = []
    at_risk = 0
    widest = None

    for table, table_cols in per_table.items():
        info = formats.get(table)
        if not info:
            continue

        dynamic = (info.row_format or "") in ("Dynamic", "Compressed")

        total = 0
        varchars = texts = 0
        for c in table_cols:
            total += column_inline_bytes(c, dynamic)
            if c.dtype in ("varchar", "char"):
                varchars += 1
            elif c.dtype in OFF_PAGE_TYPES:
                texts += 1

        pct = round(total * 100.0 / ROW_SIZE_LIMIT, 1)
        n_cols = len(table_cols)

        if pct >= 90 or n_cols >= max_columns:
            severity = "critical"
        elif pct >= warn_pct:
            severity = "warning"
        elif pct >= warn_pct / 2:
            severity = "info"
        else:
            severity = "ok"

        entry = {
            "table_name": table,
            "doctype": doctype_of(table) if table.startswith("tab") else "",
            "columns": n_cols,
            "varchar_columns": varchars,
            "text_columns": texts,
            "estimated_row_bytes": total,
            "pct_of_limit": pct,
            "row_format": info.row_format or "",
            "engine": info.engine or "",
            "rows": int(info.n_rows or 0),
            "severity": severity,
            "headroom_bytes": max(ROW_SIZE_LIMIT - total, 0),
            "note": (
                "Legacy row format keeps 768 bytes of each TEXT column inline — "
                "switching to DYNAMIC frees most of this"
                if not dynamic and texts
                else ""
            ),
        }

        if widest is None or total > widest["estimated_row_bytes"]:
            widest = entry
        if severity in ("critical", "warning"):
            at_risk += 1
            rows.append(entry)
        elif severity == "info":
            rows.append(entry)

    rows.sort(key=lambda r: r["estimated_row_bytes"], reverse=True)

    by_severity = {"critical": 0, "warning": 0, "info": 0}
    for r in rows:
        by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + 1

    return {
        "tables": rows,
        "grouped": {"by_severity": by_severity},
        "summary": {
            "tables_scanned": len(per_table),
            "at_risk": at_risk,
            "critical": by_severity["critical"],
            "warnings": by_severity["warning"],
            "listed": len(rows),
            "widest_table": widest["table_name"] if widest else "—",
            "widest_bytes": widest["estimated_row_bytes"] if widest else 0,
            "widest_pct": widest["pct_of_limit"] if widest else 0,
            "row_size_limit": ROW_SIZE_LIMIT,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_row_size_report(warn_pct: int = 60, max_columns: int = 200, fmt: str = "json"):
    guard()
    payload = audit_row_sizes(
        warn_pct=as_int(warn_pct, 60),
        max_columns=as_int(max_columns, 200),
    )
    return respond(payload, fmt, "tables", COLUMNS, "Row Size Audit")
