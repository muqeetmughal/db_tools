"""Find redundant or duplicate database indexes across tables."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
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
    "index_name",
    "columns",
    "kind",
    "duplicate_of",
    "covering_columns",
    "table_rows",
    "drop_sql",
]


def _load_indexes():
    """Return {table: {index_name: {"columns": [...], "unique": bool}}}."""
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS tbl, INDEX_NAME AS idx, COLUMN_NAME AS col,
               SEQ_IN_INDEX AS seq, NON_UNIQUE AS non_unique, INDEX_TYPE AS itype,
               SUB_PART AS sub_part
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """,
        (db_name(),),
        as_dict=True,
    )

    tables = {}
    for r in rows:
        idx = tables.setdefault(r.tbl, {}).setdefault(
            r.idx,
            {"columns": [], "unique": not r.non_unique, "type": r.itype},
        )
        col = r.col
        if r.sub_part:
            col = f"{col}({r.sub_part})"
        idx["columns"].append(col)
    return tables


def _table_rows_map():
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS name, TABLE_ROWS AS n_rows,
               INDEX_LENGTH AS index_length
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """,
        (db_name(),),
        as_dict=True,
    )
    return {r.name: r for r in rows}


def detect_duplicate_indexes(include_prefix: bool = True, include_fulltext: bool = False):
    require_mariadb()
    started = time.monotonic()

    tables = _load_indexes()
    stats = _table_rows_map()

    findings = []
    indexes_scanned = 0

    for table in sorted(tables):
        indexes = tables[table]
        info = stats.get(table)
        n_rows = int((info.n_rows if info else 0) or 0)

        # Sort widest-first so a narrow index is always compared against the
        # widest index that could already cover it.
        ordered = sorted(
            indexes.items(),
            key=lambda kv: (-len(kv[1]["columns"]), kv[0]),
        )
        indexes_scanned += len(ordered)

        consumed = set()
        for i, (name_a, a) in enumerate(ordered):
            if not include_fulltext and a["type"] in ("FULLTEXT", "SPATIAL"):
                continue
            for name_b, b in ordered[i + 1:]:
                if name_b in consumed:
                    continue
                if not include_fulltext and b["type"] in ("FULLTEXT", "SPATIAL"):
                    continue

                # PRIMARY must never be dropped, so it can only be the covering side.
                if name_b == "PRIMARY":
                    continue

                same_len = len(a["columns"]) == len(b["columns"])
                is_prefix = a["columns"][: len(b["columns"])] == b["columns"]

                if not is_prefix:
                    continue

                if same_len:
                    kind = "exact duplicate"
                    reason = f"Identical column list to `{name_a}`"
                    severity = "critical"
                else:
                    if not include_prefix:
                        continue
                    kind = "redundant prefix"
                    reason = f"Columns are a leading prefix of `{name_a}`"
                    severity = "warning"

                # A unique index is never redundant against a non-unique one.
                if b["unique"] and not a["unique"]:
                    continue

                consumed.add(name_b)
                findings.append(
                    {
                        "table_name": table,
                        "doctype": doctype_of(table) if table.startswith("tab") else "",
                        "index_name": name_b,
                        "columns": ", ".join(b["columns"]),
                        "kind": kind,
                        "severity": severity,
                        "duplicate_of": name_a,
                        "covering_columns": ", ".join(a["columns"]),
                        "unique": b["unique"],
                        "table_rows": n_rows,
                        "reason": reason,
                        "drop_sql": f"ALTER TABLE `{table}` DROP INDEX `{name_b}`;",
                    }
                )
                break

    by_kind = {}
    by_table = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_table[f["table_name"]] = by_table.get(f["table_name"], 0) + 1

    total_index_bytes = sum((s.index_length or 0) for s in stats.values())

    return {
        "findings": findings,
        "grouped": {"by_kind": by_kind, "by_table": by_table},
        "summary": {
            "tables_scanned": len(tables),
            "indexes_scanned": indexes_scanned,
            "duplicate_indexes": len(findings),
            "exact_duplicates": by_kind.get("exact duplicate", 0),
            "redundant_prefixes": by_kind.get("redundant prefix", 0),
            "affected_tables": len(by_table),
            "total_index_size": fmt_bytes(total_index_bytes),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_duplicate_indexes_report(
    include_prefix: bool = True,
    include_fulltext: bool = False,
    fmt: str = "json",
):
    guard()
    payload = detect_duplicate_indexes(
        include_prefix=as_bool(include_prefix),
        include_fulltext=as_bool(include_fulltext),
    )
    return respond(payload, fmt, "findings", COLUMNS, "Duplicate Indexes")
