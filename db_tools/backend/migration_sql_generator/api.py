"""Generate SQL to clean up detected issues, ready to review and run."""

import time

import frappe

from db_tools.backend.common import as_bool, as_int, db_name, guard, quote, respond

COLUMNS = ["category", "risk", "target", "sql", "reason"]

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _stmt(category, risk, target, sql, reason):
    return {
        "category": category,
        "risk": risk,
        "target": target,
        "sql": sql,
        "reason": reason,
    }


def generate_migration_sql(
    extra_columns: bool = True,
    duplicate_indexes: bool = True,
    missing_indexes: bool = True,
    orphan_tables: bool = False,
    orphan_children: bool = False,
    min_rows: int = 1000,
    limit: int = 2000,
):
    started = time.monotonic()

    statements = []
    errors = []

    if extra_columns:
        try:
            from db_tools.backend.schema_comparison.api import find_extra_db_columns

            for item in find_extra_db_columns():
                for column in item["extra_columns"]:
                    statements.append(
                        _stmt(
                            "Extra Columns",
                            "high",
                            f"{item['table']}.{column}",
                            f"ALTER TABLE {quote(item['table'])} DROP COLUMN {quote(column)};",
                            f"Column exists in the database but not in the {item['doctype']} schema "
                            f"({item['app']}) — dropping it destroys the stored data",
                        )
                    )
        except Exception as e:
            errors.append(f"Schema Comparison: {e}")

    if duplicate_indexes:
        try:
            from db_tools.backend.duplicate_index_detector.api import detect_duplicate_indexes

            for f in detect_duplicate_indexes()["findings"]:
                statements.append(
                    _stmt(
                        "Duplicate Indexes",
                        "low",
                        f"{f['table_name']}.{f['index_name']}",
                        f["drop_sql"],
                        f"{f['kind']} — already covered by `{f['duplicate_of']}` ({f['covering_columns']})",
                    )
                )
        except Exception as e:
            errors.append(f"Duplicate Index Detector: {e}")

    if missing_indexes:
        try:
            from db_tools.backend.missing_index_advisor.api import advise_missing_indexes

            for s in advise_missing_indexes(min_rows=min_rows)["suggestions"]:
                statements.append(
                    _stmt(
                        "Missing Indexes",
                        "low",
                        f"{s['table_name']}.{s['column_name']}",
                        s["create_sql"],
                        f"{s['priority']} priority — {s['reason']} ({s['table_rows']:,} rows)",
                    )
                )
        except Exception as e:
            errors.append(f"Missing Index Advisor: {e}")

    if orphan_tables:
        try:
            from db_tools.backend.orphan_table_finder.api import find_orphan_tables

            for t in find_orphan_tables()["orphans"]:
                statements.append(
                    _stmt(
                        "Orphan Tables",
                        "high",
                        t["table_name"],
                        t["drop_sql"],
                        f"{t['reason']} — {t['rows']:,} rows, {t['size']} would be lost",
                    )
                )
        except Exception as e:
            errors.append(f"Orphan Table Finder: {e}")

    if orphan_children:
        try:
            from db_tools.backend.orphan_child_detector.api import detect_orphan_children

            findings = detect_orphan_children(limit=limit)["findings"]
            # One DELETE per (table, parenttype) instead of one per row.
            groups = {}
            for f in findings:
                groups.setdefault((f["child_table"], f["parenttype"]), []).append(f["row_name"])

            for (table, parenttype), names in sorted(groups.items()):
                sample = ", ".join(f"'{n}'" for n in sorted(names)[:200])
                more = f"  -- and {len(names) - 200} more row(s)" if len(names) > 200 else ""
                statements.append(
                    _stmt(
                        "Orphan Child Rows",
                        "high",
                        f"{table} (parenttype={parenttype or 'blank'})",
                        f"DELETE FROM {quote(table)} WHERE `name` IN ({sample});{more}",
                        f"{len(names)} row(s) whose parent document no longer exists",
                    )
                )
        except Exception as e:
            errors.append(f"Orphan Child Detector: {e}")

    statements.sort(key=lambda s: (RISK_ORDER.get(s["risk"], 9), s["category"], s["target"]))

    by_category = {}
    by_risk = {"low": 0, "medium": 0, "high": 0}
    for s in statements:
        by_category[s["category"]] = by_category.get(s["category"], 0) + 1
        by_risk[s["risk"]] = by_risk.get(s["risk"], 0) + 1

    return {
        "statements": statements,
        "script": _build_script(statements, by_category, errors),
        "errors": errors,
        "grouped": {"by_category": by_category, "by_risk": by_risk},
        "summary": {
            "database": db_name(),
            "statements": len(statements),
            "categories": len(by_category),
            "low_risk": by_risk["low"],
            "high_risk": by_risk["high"],
            "errors": len(errors),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


def _build_script(statements, by_category, errors) -> str:
    lines = [
        "-- ---------------------------------------------------------------",
        "-- db_tools · Migration SQL",
        f"-- Database   : {db_name()}",
        f"-- Generated  : {frappe.utils.now()}",
        f"-- Statements : {len(statements)}",
        "--",
        "-- REVIEW EVERY STATEMENT AND TAKE A BACKUP FIRST.",
        "-- Nothing here has been executed. DROP/DELETE statements are",
        "-- destructive and are commented out by risk level below.",
        "-- ---------------------------------------------------------------",
        "",
    ]

    if errors:
        lines.append("-- Some scans failed and their statements are missing:")
        lines.extend(f"--   ! {e}" for e in errors)
        lines.append("")

    if not statements:
        lines.append("-- Nothing to clean up. The database matches the schema.")
        return "\n".join(lines)

    for category in sorted(by_category):
        rows = [s for s in statements if s["category"] == category]
        lines.append(f"-- === {category} ({len(rows)}) " + "=" * max(0, 46 - len(category)))
        for s in rows:
            lines.append(f"--   {s['reason']}")
            # High-risk statements ship commented out so nothing runs by accident.
            lines.append(("-- " if s["risk"] == "high" else "") + s["sql"])
            lines.append("")
        lines.append("")

    high = [s for s in statements if s["risk"] == "high"]
    if high:
        lines.append(
            f"-- {len(high)} high-risk statement(s) above are commented out. "
            "Uncomment only what you have verified."
        )

    return "\n".join(lines)


@frappe.whitelist()
def get_migration_sql(
    extra_columns: bool = True,
    duplicate_indexes: bool = True,
    missing_indexes: bool = True,
    orphan_tables: bool = False,
    orphan_children: bool = False,
    min_rows: int = 1000,
    limit: int = 2000,
    fmt: str = "json",
):
    guard()
    payload = generate_migration_sql(
        extra_columns=as_bool(extra_columns),
        duplicate_indexes=as_bool(duplicate_indexes),
        missing_indexes=as_bool(missing_indexes),
        orphan_tables=as_bool(orphan_tables),
        orphan_children=as_bool(orphan_children),
        min_rows=as_int(min_rows, 1000),
        limit=as_int(limit, 2000),
    )

    if fmt == "sql":
        return {"data": payload["script"], "format": "sql"}

    return respond(payload, fmt, "statements", COLUMNS, "Migration SQL")
