"""Find fragmented tables and audit storage engine / row format."""

import time

import frappe

from db_tools.backend.common import (
    as_int,
    db_name,
    doctype_of,
    fmt_bytes,
    guard,
    require_mariadb,
    respond,
)

COLUMNS = [
    "table_name",
    "doctype",
    "rows",
    "data_size",
    "index_size",
    "free_size",
    "fragmentation_pct",
    "engine",
    "row_format",
    "optimize_sql",
]

# Row formats that handle long values badly on InnoDB.
LEGACY_ROW_FORMATS = {"Compact", "Redundant", "Fixed"}


def _declared_engines() -> dict:
    """{doctype: engine} as declared on the DocType itself.

    A DocType may legitimately ask for MyISAM (Frappe does this for a few log
    tables), so the audit compares against what was asked for rather than
    assuming InnoDB everywhere.
    """
    out = {}
    for d in frappe.get_all("DocType", fields=["name", "engine"]):
        out[f"tab{d.name}"] = d.engine or "InnoDB"
    return out


def analyze_fragmentation(min_free_bytes: int = 1024 * 1024, min_pct: int = 10):
    require_mariadb()
    started = time.monotonic()

    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name, ENGINE AS engine, ROW_FORMAT AS row_format,
               TABLE_ROWS AS n_rows, DATA_LENGTH AS data_length,
               INDEX_LENGTH AS index_length, DATA_FREE AS data_free
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """,
        (db_name(),),
        as_dict=True,
    )

    declared = _declared_engines()

    fragmented = []
    engine_issues = []
    total_free = 0
    engines = {}
    formats = {}

    for r in rows:
        data_len = r.data_length or 0
        index_len = r.index_length or 0
        free = r.data_free or 0
        total = data_len + index_len + free
        total_free += free

        engines[r.engine or "?"] = engines.get(r.engine or "?", 0) + 1
        formats[r.row_format or "?"] = formats.get(r.row_format or "?", 0) + 1

        pct = round(free * 100.0 / total, 2) if total else 0.0

        if free >= min_free_bytes and pct >= min_pct:
            fragmented.append(
                {
                    "table_name": r.name,
                    "doctype": doctype_of(r.name) if r.name.startswith("tab") else "",
                    "rows": int(r.n_rows or 0),
                    "data_bytes": data_len,
                    "index_bytes": index_len,
                    "free_bytes": free,
                    "data_size": fmt_bytes(data_len),
                    "index_size": fmt_bytes(index_len),
                    "free_size": fmt_bytes(free),
                    "fragmentation_pct": pct,
                    "engine": r.engine or "",
                    "row_format": r.row_format or "",
                    "severity": "warning" if pct >= 30 else "info",
                    "optimize_sql": f"OPTIMIZE TABLE `{r.name}`;",
                }
            )

        # Engine / row-format problems are reported regardless of free space.
        wanted = declared.get(r.name)
        if r.engine and wanted and r.engine != wanted:
            engine_issues.append(
                {
                    "table_name": r.name,
                    "doctype": doctype_of(r.name) if r.name.startswith("tab") else "",
                    "rows": int(r.n_rows or 0),
                    "engine": r.engine,
                    "row_format": r.row_format or "",
                    "issue": f"Engine is {r.engine}, but the DocType declares {wanted}",
                    "detail": "The table drifted from what its DocType asks for",
                    "severity": "critical",
                    "fix_sql": f"ALTER TABLE `{r.name}` ENGINE={wanted};",
                }
            )
        elif r.engine and r.engine != "InnoDB":
            # Matches what was asked for — worth knowing, but not drift.
            engine_issues.append(
                {
                    "table_name": r.name,
                    "doctype": doctype_of(r.name) if r.name.startswith("tab") else "",
                    "rows": int(r.n_rows or 0),
                    "engine": r.engine,
                    "row_format": r.row_format or "",
                    "issue": f"Storage engine is {r.engine}",
                    "detail": (
                        f"Declared as {wanted} by its DocType — no transactions or "
                        "row-level locking, and no crash recovery"
                        if wanted
                        else "Framework-managed table — no transactions or crash recovery"
                    ),
                    "severity": "info",
                    "fix_sql": f"ALTER TABLE `{r.name}` ENGINE=InnoDB;",
                }
            )
        elif r.row_format in LEGACY_ROW_FORMATS:
            engine_issues.append(
                {
                    "table_name": r.name,
                    "doctype": doctype_of(r.name) if r.name.startswith("tab") else "",
                    "rows": int(r.n_rows or 0),
                    "engine": r.engine or "",
                    "row_format": r.row_format,
                    "issue": f"Row format is {r.row_format}",
                    "detail": "DYNAMIC stores long values off-page and avoids row-size errors",
                    "severity": "warning",
                    "fix_sql": f"ALTER TABLE `{r.name}` ROW_FORMAT=DYNAMIC;",
                }
            )

    fragmented.sort(key=lambda x: x["free_bytes"], reverse=True)
    engine_issues.sort(key=lambda x: (x["severity"] != "critical", -x["rows"]))

    reclaimable = sum(f["free_bytes"] for f in fragmented)

    return {
        "fragmented": fragmented,
        "engine_issues": engine_issues,
        "grouped": {"engines": engines, "row_formats": formats},
        "summary": {
            "tables_scanned": len(rows),
            "fragmented_tables": len(fragmented),
            "reclaimable": fmt_bytes(reclaimable),
            "reclaimable_bytes": reclaimable,
            "total_free": fmt_bytes(total_free),
            "engine_issues": len(engine_issues),
            "engine_drift": sum(1 for e in engine_issues if e["severity"] == "critical"),
            "non_innodb": sum(1 for e in engine_issues if e["engine"] != "InnoDB"),
            "distinct_engines": len(engines),
            "min_pct": min_pct,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_fragmentation_report(
    min_free_kb: int = 1024,
    min_pct: int = 10,
    fmt: str = "json",
):
    guard()
    payload = analyze_fragmentation(
        min_free_bytes=as_int(min_free_kb, 1024) * 1024,
        min_pct=as_int(min_pct, 10),
    )
    return respond(payload, fmt, "fragmented", COLUMNS, "Table Fragmentation")
