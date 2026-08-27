"""Suggest indexes for columns commonly used in filters and joins."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
    as_int,
    as_list,
    db_name,
    guard,
    require_mariadb,
    respond,
)

COLUMNS = [
    "table_name",
    "doctype",
    "column_name",
    "fieldtype",
    "reason",
    "priority",
    "table_rows",
    "create_sql",
]

# Fieldtypes worth indexing when used as filters.
INDEXABLE_FIELDTYPES = {"Link", "Dynamic Link", "Select", "Date", "Datetime"}


def _leading_indexed_columns() -> dict:
    """{table: {first column of each index}} — a column already leading an index."""
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND SEQ_IN_INDEX = 1
        """,
        (db_name(),),
        as_dict=True,
    )
    out = {}
    for r in rows:
        out.setdefault(r.tbl, set()).add(r.col)
    return out


def _table_info() -> dict:
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name, TABLE_ROWS AS n_rows
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """,
        (db_name(),),
        as_dict=True,
    )
    return {r.name: int(r.n_rows or 0) for r in rows}


def _columns_map() -> dict:
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        """,
        (db_name(),),
        as_dict=True,
    )
    out = {}
    for r in rows:
        out.setdefault(r.tbl, set()).add(r.col)
    return out


def _priority(n_rows: int, base: str) -> str:
    """Weight the suggestion by table size — an index on a tiny table is noise."""
    if n_rows >= 100000:
        return "high"
    if n_rows >= 10000:
        return "high" if base == "high" else "medium"
    if n_rows >= 1000:
        return "medium" if base == "high" else "low"
    return "low"


def advise_missing_indexes(
    doctype: str | None = None,
    min_rows: int = 1000,
    include_select: bool = False,
    include_child_parent: bool = True,
):
    require_mariadb()
    started = time.monotonic()

    indexed = _leading_indexed_columns()
    table_rows = _table_info()
    columns_map = _columns_map()

    only = {d.lower() for d in as_list(doctype)}

    doctypes = frappe.get_all(
        "DocType",
        filters={"issingle": 0, "is_virtual": 0},
        fields=["name", "istable", "sort_field", "module"],
        order_by="name asc",
    )

    suggestions = []
    doctypes_scanned = 0
    fields_examined = 0

    for dt in doctypes:
        if only and dt.name.lower() not in only:
            continue

        table = f"tab{dt.name}"
        if table not in table_rows:
            continue

        n_rows = table_rows[table]
        if n_rows < min_rows:
            continue

        doctypes_scanned += 1
        have = indexed.get(table, set())
        cols = columns_map.get(table, set())

        try:
            meta = frappe.get_meta(dt.name)
        except Exception:
            continue

        seen = set()

        for df in meta.fields:
            fields_examined += 1
            fieldname = df.fieldname
            if not fieldname or fieldname in seen or fieldname not in cols:
                continue

            base = None
            reason = ""

            if df.get("search_index"):
                base, reason = "high", "Field is flagged search_index but has no database index"
            elif df.fieldtype == "Link":
                base, reason = "high", f"Link field to {df.options or '?'} — used in filters and joins"
            elif df.fieldtype == "Dynamic Link":
                base, reason = "medium", "Dynamic Link field — filtered together with its doctype field"
            elif df.fieldtype in ("Date", "Datetime") and df.get("in_standard_filter"):
                base, reason = "medium", "Date field exposed as a standard filter"
            elif include_select and df.fieldtype == "Select":
                base, reason = "low", "Select field — often used to filter by status"

            if not base or fieldname in have:
                continue

            seen.add(fieldname)
            suggestions.append(
                {
                    "table_name": table,
                    "doctype": dt.name,
                    "module": dt.module or "",
                    "column_name": fieldname,
                    "fieldtype": df.fieldtype,
                    "reason": reason,
                    "priority": _priority(n_rows, base),
                    "table_rows": n_rows,
                    "create_sql": f"ALTER TABLE `{table}` ADD INDEX `{fieldname}` (`{fieldname}`);",
                }
            )

        # Child tables are always joined on parent.
        if include_child_parent and dt.istable and "parent" in cols and "parent" not in have:
            suggestions.append(
                {
                    "table_name": table,
                    "doctype": dt.name,
                    "module": dt.module or "",
                    "column_name": "parent",
                    "fieldtype": "Data",
                    "reason": "Child table joined on `parent` for every parent document load",
                    "priority": _priority(n_rows, "high"),
                    "table_rows": n_rows,
                    "create_sql": f"ALTER TABLE `{table}` ADD INDEX `parent` (`parent`);",
                }
            )

        # The default list-view ordering column.
        sort_field = (dt.sort_field or "modified").split(",")[0].strip()
        if sort_field and sort_field in cols and sort_field not in have and sort_field not in seen:
            suggestions.append(
                {
                    "table_name": table,
                    "doctype": dt.name,
                    "module": dt.module or "",
                    "column_name": sort_field,
                    "fieldtype": "Sort Field",
                    "reason": f"Default list ordering is `{sort_field}` — every list view sorts on it",
                    "priority": _priority(n_rows, "medium"),
                    "table_rows": n_rows,
                    "create_sql": f"ALTER TABLE `{table}` ADD INDEX `{sort_field}` (`{sort_field}`);",
                }
            )

    rank = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: (rank.get(s["priority"], 3), -s["table_rows"], s["table_name"]))

    by_priority = {"high": 0, "medium": 0, "low": 0}
    by_doctype = {}
    for s in suggestions:
        by_priority[s["priority"]] = by_priority.get(s["priority"], 0) + 1
        by_doctype[s["doctype"]] = by_doctype.get(s["doctype"], 0) + 1

    return {
        "suggestions": suggestions,
        "grouped": {"by_priority": by_priority, "by_doctype": by_doctype},
        "summary": {
            "doctypes_scanned": doctypes_scanned,
            "fields_examined": fields_examined,
            "suggestions": len(suggestions),
            "high_priority": by_priority["high"],
            "affected_doctypes": len(by_doctype),
            "min_rows": min_rows,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_missing_indexes_report(
    doctype: str | None = None,
    min_rows: int = 1000,
    include_select: bool = False,
    include_child_parent: bool = True,
    fmt: str = "json",
):
    guard()
    payload = advise_missing_indexes(
        doctype=doctype or None,
        min_rows=as_int(min_rows, 1000),
        include_select=as_bool(include_select),
        include_child_parent=as_bool(include_child_parent),
    )
    return respond(payload, fmt, "suggestions", COLUMNS, "Missing Index Suggestions")
