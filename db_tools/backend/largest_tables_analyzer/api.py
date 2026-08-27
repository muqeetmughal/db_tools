"""Analyze which tables consume the most space in the database."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
    as_int,
    db_name,
    doctype_of,
    fmt_bytes,
    guard,
    quote,
    require_mariadb,
    respond,
)

COLUMNS = [
    "table_name",
    "doctype",
    "rows",
    "data_size",
    "index_size",
    "total_size",
    "pct_of_db",
    "avg_row_size",
    "engine",
]


def analyze_largest_tables(limit: int = 50, exact_counts: bool = False, min_bytes: int = 0):
    require_mariadb()
    started = time.monotonic()

    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name,
               ENGINE AS engine,
               TABLE_ROWS AS n_rows,
               DATA_LENGTH AS data_length,
               INDEX_LENGTH AS index_length,
               DATA_FREE AS data_free,
               TABLE_COLLATION AS collation
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY (DATA_LENGTH + INDEX_LENGTH) DESC
        """,
        (db_name(),),
        as_dict=True,
    )

    total_data = sum(r.data_length or 0 for r in rows)
    total_index = sum(r.index_length or 0 for r in rows)
    total_free = sum(r.data_free or 0 for r in rows)
    total_size = total_data + total_index
    total_rows = 0

    out = []
    for r in rows:
        data_len = r.data_length or 0
        index_len = r.index_length or 0
        size = data_len + index_len
        if size < min_bytes:
            continue

        n_rows = int(r.n_rows or 0)
        if exact_counts:
            try:
                n_rows = frappe.db.sql(f"SELECT COUNT(*) FROM {quote(r.name)}")[0][0]
            except Exception:
                pass
        total_rows += n_rows

        out.append(
            {
                "table_name": r.name,
                "doctype": doctype_of(r.name) if r.name.startswith("tab") else "",
                "rows": n_rows,
                "data_bytes": data_len,
                "index_bytes": index_len,
                "total_bytes": size,
                "data_size": fmt_bytes(data_len),
                "index_size": fmt_bytes(index_len),
                "total_size": fmt_bytes(size),
                "free_size": fmt_bytes(r.data_free or 0),
                "pct_of_db": round(size * 100.0 / total_size, 2) if total_size else 0.0,
                "avg_row_size": fmt_bytes(size / n_rows) if n_rows else "—",
                "index_ratio": round(index_len * 100.0 / size, 1) if size else 0.0,
                "engine": r.engine or "",
                "collation": r.collation or "",
            }
        )

    out.sort(key=lambda x: x["total_bytes"], reverse=True)
    if limit:
        out = out[:limit]

    biggest = out[0] if out else None

    return {
        "tables": out,
        "summary": {
            "table_count": len(rows),
            "shown": len(out),
            "total_bytes": total_size,
            "total_size": fmt_bytes(total_size),
            "data_size": fmt_bytes(total_data),
            "index_size": fmt_bytes(total_index),
            "free_size": fmt_bytes(total_free),
            "total_rows": total_rows,
            "largest_table": biggest["table_name"] if biggest else "—",
            "largest_size": biggest["total_size"] if biggest else "—",
            "database": db_name(),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_largest_tables_report(
    limit: int = 50,
    exact_counts: bool = False,
    min_bytes: int = 0,
    fmt: str = "json",
):
    guard()
    payload = analyze_largest_tables(
        limit=as_int(limit, 50),
        exact_counts=as_bool(exact_counts),
        min_bytes=as_int(min_bytes, 0),
    )
    return respond(payload, fmt, "tables", COLUMNS, "Largest Tables")
