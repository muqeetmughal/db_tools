"""Find columns that are always empty or hold a single value."""

import time

import frappe

from db_tools.backend.common import (
    STANDARD_COLUMNS_ALWAYS_SET,
    as_bool,
    as_int,
    as_list,
    db_name,
    doctype_of,
    guard,
    quote,
    require_mariadb,
    respond,
)

COLUMNS = [
    "table_name",
    "doctype",
    "column_name",
    "column_type",
    "kind",
    "non_empty_rows",
    "distinct_values",
    "sole_value",
    "table_rows",
]

# Types where '' is a meaningful "blank"; everything else only checks NULL.
STRING_TYPES = {"varchar", "char", "text", "tinytext", "mediumtext", "longtext"}

# COUNT(DISTINCT ...) on these builds large temp tables — skip the distinct pass.
NO_DISTINCT_TYPES = {"text", "mediumtext", "longtext", "blob", "longblob", "json"}

# Columns are chunked so a wide table does not build one enormous SELECT.
CHUNK = 40


def _candidate_tables(min_rows: int, max_tables: int, only: set) -> list:
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name, TABLE_ROWS AS n_rows
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' AND TABLE_NAME LIKE 'tab%%'
        ORDER BY TABLE_ROWS DESC
        """,
        (db_name(),),
        as_dict=True,
    )

    out = []
    for r in rows:
        if only and doctype_of(r.name).lower() not in only:
            continue
        if (r.n_rows or 0) < min_rows:
            continue
        out.append(r)
        if max_tables and len(out) >= max_tables:
            break
    return out


def _columns_of(table: str) -> list:
    return frappe.db.sql(
        """
        SELECT COLUMN_NAME AS col, DATA_TYPE AS dtype, COLUMN_TYPE AS ctype
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (db_name(), table),
        as_dict=True,
    )


def _non_empty_expr(col) -> str:
    """SQL counting rows where this column actually holds something."""
    name = quote(col.col)
    if col.dtype in STRING_TYPES:
        return f"SUM(CASE WHEN {name} IS NOT NULL AND {name} <> '' THEN 1 ELSE 0 END)"
    return f"SUM(CASE WHEN {name} IS NOT NULL THEN 1 ELSE 0 END)"


def analyze_empty_columns(
    doctype: str | None = None,
    min_rows: int = 100,
    max_tables: int = 40,
    include_standard: bool = False,
    find_single_value: bool = True,
):
    require_mariadb()
    started = time.monotonic()

    only = {d.lower() for d in as_list(doctype)}
    tables = _candidate_tables(min_rows, max_tables, only)

    findings = []
    tables_scanned = 0
    columns_scanned = 0
    queries = 0

    for entry in tables:
        table = entry.name
        cols = [
            c
            for c in _columns_of(table)
            if include_standard or c.col not in STANDARD_COLUMNS_ALWAYS_SET
        ]
        if not cols:
            continue

        # Exact count — the estimate is not good enough to call a column empty.
        total = frappe.db.sql(f"SELECT COUNT(*) FROM {quote(table)}")[0][0]
        queries += 1
        if not total or total < min_rows:
            continue

        tables_scanned += 1
        columns_scanned += len(cols)

        stats = {}
        for i in range(0, len(cols), CHUNK):
            chunk = cols[i : i + CHUNK]
            selects = []
            for n, col in enumerate(chunk):
                selects.append(f"{_non_empty_expr(col)} AS ne_{n}")
                if find_single_value and col.dtype not in NO_DISTINCT_TYPES:
                    selects.append(f"COUNT(DISTINCT {quote(col.col)}) AS dv_{n}")

            row = frappe.db.sql(
                f"SELECT {', '.join(selects)} FROM {quote(table)}", as_dict=True
            )[0]
            queries += 1

            for n, col in enumerate(chunk):
                stats[col.col] = {
                    "col": col,
                    "non_empty": int(row.get(f"ne_{n}") or 0),
                    "distinct": row.get(f"dv_{n}"),
                }

        for name, s in stats.items():
            col = s["col"]
            non_empty = s["non_empty"]
            distinct = s["distinct"]

            if non_empty == 0:
                kind, severity, sole = "always empty", "warning", ""
            elif find_single_value and distinct == 1 and non_empty == total:
                sole = frappe.db.sql(
                    f"SELECT {quote(name)} FROM {quote(table)} LIMIT 1"
                )[0][0]
                queries += 1
                kind, severity = "single value", "info"
                sole = str(sole)[:80]
            else:
                continue

            findings.append(
                {
                    "table_name": table,
                    "doctype": doctype_of(table),
                    "column_name": name,
                    "column_type": col.ctype,
                    "kind": kind,
                    "severity": severity,
                    "non_empty_rows": non_empty,
                    "distinct_values": distinct if distinct is not None else "—",
                    "sole_value": sole,
                    "table_rows": total,
                    "fill_rate": round(non_empty * 100.0 / total, 2) if total else 0.0,
                    "drop_sql": f"ALTER TABLE `{table}` DROP COLUMN `{name}`;",
                }
            )

    by_kind = {}
    by_table = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_table[f["table_name"]] = by_table.get(f["table_name"], 0) + 1

    return {
        "findings": findings,
        "grouped": {"by_kind": by_kind, "by_table": by_table},
        "summary": {
            "tables_scanned": tables_scanned,
            "columns_scanned": columns_scanned,
            "always_empty": by_kind.get("always empty", 0),
            "single_value": by_kind.get("single value", 0),
            "affected_tables": len(by_table),
            "queries_run": queries,
            "min_rows": min_rows,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_empty_columns_report(
    doctype: str | None = None,
    min_rows: int = 100,
    max_tables: int = 40,
    include_standard: bool = False,
    find_single_value: bool = True,
    fmt: str = "json",
):
    guard()
    payload = analyze_empty_columns(
        doctype=doctype or None,
        min_rows=as_int(min_rows, 100),
        max_tables=as_int(max_tables, 40),
        include_standard=as_bool(include_standard),
        find_single_value=as_bool(find_single_value),
    )
    return respond(payload, fmt, "findings", COLUMNS, "Empty Columns")
