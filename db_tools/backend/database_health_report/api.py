"""Generate an overall health report for the site database."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
    as_int,
    db_name,
    fmt_bytes,
    guard,
    require_mariadb,
    respond,
)

COLUMNS = ["check", "category", "status", "value", "message", "tool"]

# status -> points deducted per check
PENALTY = {"critical": 25, "warning": 10, "ok": 0, "skipped": 0}


def _check(name, category, status, value, message, tool="", route=""):
    return {
        "check": name,
        "category": category,
        "status": status,
        "value": value,
        "message": message,
        "tool": tool,
        "route": route,
    }


def _safe(fn, *args, **kwargs):
    """Run a sub-scan, converting failures into a reportable error."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "db_tools health report")
        return None, str(e)


def build_health_report(deep: bool = False, link_limit: int = 500):
    require_mariadb()
    started = time.monotonic()

    from db_tools.backend.duplicate_index_detector.api import detect_duplicate_indexes
    from db_tools.backend.largest_tables_analyzer.api import analyze_largest_tables
    from db_tools.backend.missing_index_advisor.api import advise_missing_indexes
    from db_tools.backend.orphan_table_finder.api import find_orphan_tables
    from db_tools.backend.schema_comparison.api import find_extra_db_columns

    checks = []
    errors = []

    # -- size ---------------------------------------------------------------
    sizes, err = _safe(analyze_largest_tables, limit=10)
    if err:
        errors.append(("Largest Tables Analyzer", err))
        sizes = {"tables": [], "summary": {}}

    size_summary = sizes["summary"]
    total_bytes = size_summary.get("total_bytes", 0)
    checks.append(
        _check(
            "Database size",
            "Storage",
            "ok",
            size_summary.get("total_size", "—"),
            f"{size_summary.get('table_count', 0)} tables, "
            f"{size_summary.get('total_rows', 0):,} estimated rows",
            "Largest Tables Analyzer",
            "/db_tools/largest_tables_analyzer",
        )
    )

    top = sizes["tables"][0] if sizes["tables"] else None
    if top:
        status = "warning" if top["pct_of_db"] > 50 else "ok"
        checks.append(
            _check(
                "Largest table share",
                "Storage",
                status,
                f"{top['pct_of_db']}%",
                f"`{top['table_name']}` uses {top['total_size']} of the database",
                "Largest Tables Analyzer",
                "/db_tools/largest_tables_analyzer",
            )
        )

    # -- orphan tables ------------------------------------------------------
    orphan_tables, err = _safe(find_orphan_tables)
    if err:
        errors.append(("Orphan Table Finder", err))
        orphan_tables = {"orphans": [], "missing": [], "summary": {}}

    n_orphan = orphan_tables["summary"].get("orphan_tables", 0)
    checks.append(
        _check(
            "Orphan tables",
            "Schema",
            "warning" if n_orphan else "ok",
            n_orphan,
            f"{n_orphan} table(s) with no matching DocType "
            f"({orphan_tables['summary'].get('reclaimable', '0 B')} reclaimable)"
            if n_orphan
            else "Every table maps to an installed DocType",
            "Orphan Table Finder",
            "/db_tools/orphan_table_finder",
        )
    )

    n_missing = orphan_tables["summary"].get("missing_tables", 0)
    checks.append(
        _check(
            "Missing tables",
            "Schema",
            "critical" if n_missing else "ok",
            n_missing,
            f"{n_missing} DocType(s) have no table — run `bench migrate`"
            if n_missing
            else "Every DocType has its table",
            "Orphan Table Finder",
            "/db_tools/orphan_table_finder",
        )
    )

    # -- extra columns ------------------------------------------------------
    extra, err = _safe(find_extra_db_columns)
    if err:
        errors.append(("Schema Comparison", err))
        extra = []

    n_extra = sum(len(item["extra_columns"]) for item in (extra or []))
    checks.append(
        _check(
            "Extra database columns",
            "Schema",
            "warning" if n_extra else "ok",
            n_extra,
            f"{n_extra} column(s) across {len(extra or [])} DocType(s) exist in the "
            "database but not in any schema"
            if n_extra
            else "Database columns match the DocType schemas",
            "Schema Comparison",
            "/db_tools/schema_comparison",
        )
    )

    # -- indexes ------------------------------------------------------------
    dupes, err = _safe(detect_duplicate_indexes)
    if err:
        errors.append(("Duplicate Index Detector", err))
        dupes = {"findings": [], "summary": {}}

    n_dupes = dupes["summary"].get("duplicate_indexes", 0)
    n_exact = dupes["summary"].get("exact_duplicates", 0)
    checks.append(
        _check(
            "Duplicate indexes",
            "Indexes",
            "warning" if n_exact else ("warning" if n_dupes else "ok"),
            n_dupes,
            f"{n_exact} exact duplicate(s) and "
            f"{dupes['summary'].get('redundant_prefixes', 0)} redundant prefix index(es)"
            if n_dupes
            else "No duplicate or redundant indexes",
            "Duplicate Index Detector",
            "/db_tools/duplicate_index_detector",
        )
    )

    advice, err = _safe(advise_missing_indexes, min_rows=1000)
    if err:
        errors.append(("Missing Index Advisor", err))
        advice = {"suggestions": [], "summary": {}}

    n_high = advice["summary"].get("high_priority", 0)
    checks.append(
        _check(
            "Missing indexes",
            "Indexes",
            "warning" if n_high else "ok",
            advice["summary"].get("suggestions", 0),
            f"{n_high} high-priority index suggestion(s) on tables with 1,000+ rows"
            if n_high
            else (
                f"{advice['summary'].get('suggestions', 0)} low/medium suggestion(s) — nothing urgent"
                if advice["summary"].get("suggestions")
                else "Frequently filtered columns are indexed"
            ),
            "Missing Index Advisor",
            "/db_tools/missing_index_advisor",
        )
    )

    # -- deep scans ---------------------------------------------------------
    if deep:
        from db_tools.backend.broken_link_detector.broken_links import detect_broken_links
        from db_tools.backend.orphan_child_detector.api import detect_orphan_children

        orphan_rows, err = _safe(detect_orphan_children, limit=link_limit)
        if err:
            errors.append(("Orphan Child Detector", err))
            orphan_rows = {"summary": {}}

        n_rows = orphan_rows["summary"].get("orphan_rows", 0)
        checks.append(
            _check(
                "Orphan child rows",
                "Data Integrity",
                "critical" if n_rows else "ok",
                n_rows,
                f"{n_rows} child row(s) whose parent document no longer exists"
                if n_rows
                else "Every child row has a live parent",
                "Orphan Child Detector",
                "/db_tools/orphan_child_detector",
            )
        )

        links, err = _safe(detect_broken_links, limit=link_limit)
        if err:
            errors.append(("Broken Link Detector", err))
            links = None

        if links is not None:
            report = links.to_dict()
            n_broken = report["summary"]["broken_links"]
            checks.append(
                _check(
                    "Broken links",
                    "Data Integrity",
                    "critical" if n_broken else "ok",
                    n_broken,
                    f"{n_broken} Link/Dynamic Link value(s) point at a missing document"
                    if n_broken
                    else "All checked references resolve",
                    "Broken Link Detector",
                    "/db_tools/broken_link_detector",
                )
            )
    else:
        for name in ("Orphan child rows", "Broken links"):
            checks.append(
                _check(
                    name,
                    "Data Integrity",
                    "skipped",
                    "—",
                    "Enable “Deep scan” to include this check",
                    "",
                    "",
                )
            )

    for tool, message in errors:
        checks.append(_check(f"{tool} failed", "Errors", "critical", "error", message, tool, ""))

    score = 100
    counts = {"ok": 0, "warning": 0, "critical": 0, "skipped": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
        score -= PENALTY.get(c["status"], 0)
    score = max(score, 0)

    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"

    return {
        "checks": checks,
        "top_tables": sizes["tables"][:10],
        "summary": {
            "database": db_name(),
            "score": score,
            "grade": grade,
            "total_size": size_summary.get("total_size", "—"),
            "table_count": size_summary.get("table_count", 0),
            "checks_run": len(checks),
            "ok": counts["ok"],
            "warnings": counts["warning"],
            "critical": counts["critical"],
            "skipped": counts["skipped"],
            "deep": bool(deep),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_health_report(deep: bool = False, link_limit: int = 500, fmt: str = "json"):
    guard()
    payload = build_health_report(deep=as_bool(deep), link_limit=as_int(link_limit, 500))
    return respond(payload, fmt, "checks", COLUMNS, "Database Health Report")
