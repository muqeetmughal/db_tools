"""Analyze Frappe log/history table growth and retention."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
    as_int,
    db_name,
    fmt_bytes,
    guard,
    quote,
    require_mariadb,
    respond,
)

COLUMNS = [
    "doctype",
    "table_name",
    "rows",
    "size",
    "oldest",
    "newest",
    "span_days",
    "rows_per_day",
    "retention_days",
    "rows_beyond_retention",
    "recommendation",
]

# Log-style DocTypes shipped by Frappe/ERPNext. Anything configured in Log
# Settings is added to this at runtime.
KNOWN_LOG_DOCTYPES = [
    "Version",
    "Error Log",
    "Activity Log",
    "Access Log",
    "View Log",
    "Route History",
    "Email Queue",
    "Email Queue Recipient",
    "Scheduled Job Log",
    "Notification Log",
    "Integration Request",
    "Webhook Request Log",
    "Prepared Report",
    "Data Import Log",
    "Console Log",
    "Submission Queue",
    "Transaction Log",
    "Energy Point Log",
    "Unhandled Email",
    "Document Follow",
    "Error Snapshot",
]



def _configured_retention() -> dict:
    """{doctype: days} from Log Settings, if the site has it configured."""
    out = {}
    if not frappe.db.exists("DocType", "Logs To Clear"):
        return out
    try:
        for row in frappe.get_all(
            "Logs To Clear", fields=["ref_doctype", "days"], limit=500
        ):
            if row.ref_doctype:
                out[row.ref_doctype] = int(row.days or 0)
    except Exception:
        pass
    return out


def _table_sizes() -> dict:
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name, TABLE_ROWS AS n_rows,
               DATA_LENGTH AS data_length, INDEX_LENGTH AS index_length
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """,
        (db_name(),),
        as_dict=True,
    )
    return {r.name: r for r in rows}


def analyze_log_tables(
    default_retention: int = 90,
    include_empty: bool = False,
    exact_counts: bool = True,
):
    require_mariadb()
    started = time.monotonic()

    sizes = _table_sizes()
    retention = _configured_retention()

    # Everything Log Settings knows about counts as a log table too.
    candidates = list(dict.fromkeys(KNOWN_LOG_DOCTYPES + list(retention)))

    rows = []
    total_bytes = 0
    total_rows = 0
    reclaimable_rows = 0

    for doctype in sorted(candidates):
        table = f"tab{doctype}"
        info = sizes.get(table)
        if not info:
            continue

        n_rows = int(info.n_rows or 0)
        if exact_counts:
            try:
                n_rows = frappe.db.sql(f"SELECT COUNT(*) FROM {quote(table)}")[0][0]
            except Exception:
                pass

        if not n_rows and not include_empty:
            continue

        size = (info.data_length or 0) + (info.index_length or 0)
        total_bytes += size
        total_rows += n_rows

        oldest = newest = None
        span_days = 0
        try:
            bounds = frappe.db.sql(
                f"SELECT MIN(`creation`) AS oldest, MAX(`creation`) AS newest FROM {quote(table)}",
                as_dict=True,
            )[0]
            oldest, newest = bounds.oldest, bounds.newest
            if oldest and newest:
                span_days = max((newest - oldest).days, 0)
        except Exception:
            pass

        keep_days = retention.get(doctype, default_retention)
        beyond = 0
        if keep_days and oldest:
            try:
                beyond = frappe.db.sql(
                    f"SELECT COUNT(*) FROM {quote(table)} WHERE `creation` < %s",
                    (frappe.utils.add_days(frappe.utils.nowdate(), -keep_days),),
                )[0][0]
            except Exception:
                beyond = 0
        reclaimable_rows += beyond

        per_day = round(n_rows / span_days, 1) if span_days else float(n_rows)

        if beyond and n_rows:
            pct = beyond * 100.0 / n_rows
            severity = "critical" if pct >= 75 else "warning"
            recommendation = (
                f"{beyond:,} row(s) older than {keep_days} days "
                f"({pct:.0f}% of the table) can be cleared"
            )
        elif n_rows > 500000:
            severity = "warning"
            recommendation = "Very large log table — consider a shorter retention"
        else:
            severity = "ok"
            recommendation = "Within retention"

        rows.append(
            {
                "doctype": doctype,
                "table_name": table,
                "rows": n_rows,
                "size_bytes": size,
                "size": fmt_bytes(size),
                "oldest": str(oldest or "—")[:19],
                "newest": str(newest or "—")[:19],
                "span_days": span_days,
                "rows_per_day": per_day,
                "retention_days": keep_days or "—",
                "retention_source": "Log Settings" if doctype in retention else "default",
                "rows_beyond_retention": beyond,
                "severity": severity,
                "recommendation": recommendation,
                "clear_command": (
                    f'bench --site {frappe.local.site} clear-log-table '
                    f'--doctype "{doctype}" --days {keep_days}'
                    if keep_days
                    else ""
                ),
            }
        )

    rows.sort(key=lambda r: r["size_bytes"], reverse=True)

    by_severity = {"critical": 0, "warning": 0, "ok": 0}
    for r in rows:
        by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + 1

    db_total = sum((s.data_length or 0) + (s.index_length or 0) for s in sizes.values())

    return {
        "tables": rows,
        "grouped": {"by_severity": by_severity},
        "summary": {
            "log_tables": len(rows),
            "total_rows": total_rows,
            "total_size": fmt_bytes(total_bytes),
            "total_bytes": total_bytes,
            "pct_of_db": round(total_bytes * 100.0 / db_total, 2) if db_total else 0.0,
            "rows_beyond_retention": reclaimable_rows,
            "needs_attention": by_severity["critical"] + by_severity["warning"],
            "default_retention": default_retention,
            "configured_in_log_settings": len(retention),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_log_tables_report(
    default_retention: int = 90,
    include_empty: bool = False,
    exact_counts: bool = True,
    fmt: str = "json",
):
    guard()
    payload = analyze_log_tables(
        default_retention=as_int(default_retention, 90),
        include_empty=as_bool(include_empty),
        exact_counts=as_bool(exact_counts),
    )
    return respond(payload, fmt, "tables", COLUMNS, "Log Table Growth")
