"""Audit table and column charsets/collations for consistency."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
    db_name,
    doctype_of,
    guard,
    require_mariadb,
    respond,
)

COLUMNS = ["level", "table_name", "doctype", "column_name", "charset", "collation", "expected", "severity"]

# The charset every modern Frappe site should be on.
PREFERRED_CHARSET = "utf8mb4"

# MariaDB implements JSON as LONGTEXT ... COLLATE utf8mb4_bin, so a binary
# collation on a longtext column is the engine's own doing, not drift.
JSON_BACKING_TYPES = {"longtext", "json"}
BINARY_COLLATION_SUFFIX = "_bin"


def _db_defaults() -> dict:
    row = frappe.db.sql(
        """
        SELECT DEFAULT_CHARACTER_SET_NAME AS charset, DEFAULT_COLLATION_NAME AS collation
        FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s
        """,
        (db_name(),),
        as_dict=True,
    )
    return row[0] if row else {"charset": PREFERRED_CHARSET, "collation": ""}


def audit_collations(check_columns: bool = True, only_doctype_tables: bool = False):
    require_mariadb()
    started = time.monotonic()

    defaults = _db_defaults()
    db_collation = defaults["collation"]
    db_charset = defaults["charset"]

    like = " AND TABLE_NAME LIKE 'tab%'" if only_doctype_tables else ""

    tables = frappe.db.sql(
        f"""
        SELECT TABLE_NAME AS name, TABLE_COLLATION AS collation
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'{like}
        """,
        (db_name(),),
        as_dict=True,
    )

    findings = []
    table_collations = {}
    collation_counts = {}

    for t in tables:
        collation = t.collation or ""
        table_collations[t.name] = collation
        collation_counts[collation] = collation_counts.get(collation, 0) + 1

        charset = collation.split("_")[0] if collation else ""

        if charset and charset != PREFERRED_CHARSET:
            severity, expected = "critical", f"{PREFERRED_CHARSET} (e.g. {db_collation})"
        elif db_collation and collation != db_collation:
            severity, expected = "warning", db_collation
        else:
            continue

        findings.append(
            {
                "level": "table",
                "table_name": t.name,
                "doctype": doctype_of(t.name) if t.name.startswith("tab") else "",
                "column_name": "",
                "charset": charset,
                "collation": collation,
                "expected": expected,
                "severity": severity,
                "fix_sql": (
                    f"ALTER TABLE `{t.name}` CONVERT TO CHARACTER SET {PREFERRED_CHARSET} "
                    f"COLLATE {db_collation or PREFERRED_CHARSET + '_unicode_ci'};"
                ),
            }
        )

    columns_scanned = 0
    if check_columns:
        cols = frappe.db.sql(
            f"""
            SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col, COLUMN_TYPE AS ctype,
                   DATA_TYPE AS dtype, CHARACTER_SET_NAME AS charset,
                   COLLATION_NAME AS collation
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND COLLATION_NAME IS NOT NULL
            """,
            (db_name(),),
            as_dict=True,
        )

        for c in cols:
            if only_doctype_tables and not c.tbl.startswith("tab"):
                continue
            columns_scanned += 1

            table_collation = table_collations.get(c.tbl)
            if table_collation is None:
                continue

            if c.charset and c.charset != PREFERRED_CHARSET:
                # A different charset is the case that actually breaks joins
                # and silently mangles non-ASCII text.
                severity, expected = "critical", PREFERRED_CHARSET
            elif table_collation and c.collation != table_collation:
                if (
                    c.dtype in JSON_BACKING_TYPES
                    and (c.collation or "").endswith(BINARY_COLLATION_SUFFIX)
                ):
                    # MariaDB's own representation of a JSON column.
                    continue
                # Same charset, different collation: comparisons still work but
                # sort/compare differently, so this is worth knowing, not urgent.
                severity, expected = "info", table_collation
            else:
                continue

            findings.append(
                {
                    "level": "column",
                    "table_name": c.tbl,
                    "doctype": doctype_of(c.tbl) if c.tbl.startswith("tab") else "",
                    "column_name": c.col,
                    "charset": c.charset or "",
                    "collation": c.collation or "",
                    "expected": expected,
                    "severity": severity,
                    "fix_sql": (
                        f"ALTER TABLE `{c.tbl}` MODIFY `{c.col}` {c.ctype} "
                        f"CHARACTER SET {PREFERRED_CHARSET} COLLATE {table_collation};"
                    ),
                }
            )

    by_severity = {"critical": 0, "warning": 0, "info": 0}
    by_level = {}
    for f in findings:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        by_level[f["level"]] = by_level.get(f["level"], 0) + 1

    return {
        "findings": findings,
        "grouped": {"by_severity": by_severity, "by_level": by_level, "collations": collation_counts},
        "summary": {
            "database": db_name(),
            "db_charset": db_charset,
            "db_collation": db_collation,
            "tables_scanned": len(tables),
            "columns_scanned": columns_scanned,
            "distinct_collations": len(collation_counts),
            "issues": len(findings),
            "table_issues": by_level.get("table", 0),
            "column_issues": by_level.get("column", 0),
            "consistent": not findings,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_collation_report(
    check_columns: bool = True,
    only_doctype_tables: bool = False,
    fmt: str = "json",
):
    guard()
    payload = audit_collations(
        check_columns=as_bool(check_columns),
        only_doctype_tables=as_bool(only_doctype_tables),
    )
    return respond(payload, fmt, "findings", COLUMNS, "Charset & Collation Audit")
