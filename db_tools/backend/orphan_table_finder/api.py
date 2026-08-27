"""Find database tables that belong to no installed DocType (and vice versa)."""

import time

import frappe

from db_tools.backend.common import (
    KNOWN_SYSTEM_TABLES,
    as_bool,
    db_name,
    doctype_of,
    fmt_bytes,
    guard,
    require_mariadb,
    respond,
)

COLUMNS = ["table_name", "guessed_doctype", "kind", "rows", "size", "engine", "reason"]

# DocTypes that never get their own table.
VIRTUAL_HINTS = ("is_virtual", "issingle")


def _table_stats() -> dict:
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name,
               ENGINE AS engine,
               TABLE_ROWS AS n_rows,
               DATA_LENGTH AS data_length,
               INDEX_LENGTH AS index_length,
               CREATE_TIME AS created
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """,
        (db_name(),),
        as_dict=True,
    )
    return {r.name: r for r in rows}


def find_orphan_tables(include_non_tab: bool = True, include_missing_tables: bool = True):
    require_mariadb()
    started = time.monotonic()

    stats = _table_stats()

    doctypes = frappe.get_all(
        "DocType",
        fields=["name", "issingle", "is_virtual", "module", "istable"],
    )
    known = {d.name for d in doctypes}
    tabled = {d.name for d in doctypes if not d.issingle and not d.is_virtual}

    orphans = []
    total_orphan_bytes = 0

    for table, info in sorted(stats.items()):
        if table.startswith("tab"):
            guessed = doctype_of(table)
            if guessed in known:
                continue
            kind = "orphan doctype table"
            reason = f"No DocType named '{guessed}' exists on this site"
        else:
            if not include_non_tab:
                continue
            guessed = ""
            if table in KNOWN_SYSTEM_TABLES:
                continue
            kind = "non-doctype table"
            reason = "Table does not follow the `tab<DocType>` convention"

        size = (info.data_length or 0) + (info.index_length or 0)
        total_orphan_bytes += size
        orphans.append(
            {
                "table_name": table,
                "guessed_doctype": guessed,
                "kind": kind,
                "rows": int(info.n_rows or 0),
                "size_bytes": size,
                "size": fmt_bytes(size),
                "engine": info.engine or "",
                "created": str(info.created or ""),
                "reason": reason,
                "drop_sql": f"DROP TABLE `{table}`;",
            }
        )

    missing = []
    if include_missing_tables:
        for name in sorted(tabled):
            if f"tab{name}" not in stats:
                dt = next(d for d in doctypes if d.name == name)
                missing.append(
                    {
                        "table_name": f"tab{name}",
                        "guessed_doctype": name,
                        "kind": "missing table",
                        "module": dt.module or "",
                        "is_child_table": bool(dt.istable),
                        "reason": "DocType exists but its table is missing from the database",
                    }
                )

    return {
        "orphans": orphans,
        "missing": missing,
        "summary": {
            "tables_in_db": len(stats),
            "doctypes": len(known),
            "orphan_tables": len(orphans),
            "missing_tables": len(missing),
            "reclaimable_bytes": total_orphan_bytes,
            "reclaimable": fmt_bytes(total_orphan_bytes),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_orphan_tables_report(
    include_non_tab: bool = True,
    include_missing_tables: bool = True,
    fmt: str = "json",
):
    guard()
    payload = find_orphan_tables(
        include_non_tab=as_bool(include_non_tab),
        include_missing_tables=as_bool(include_missing_tables),
    )
    return respond(payload, fmt, "orphans", COLUMNS, "Orphan Tables")
