"""Audit the schema against MariaDB/InnoDB hard limits."""

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
from db_tools.backend.row_size_auditor.api import column_inline_bytes

COLUMNS = [
    "check",
    "object_type",
    "table_name",
    "object_name",
    "actual",
    "limit",
    "usage_pct",
    "severity",
    "suggestion",
]

# --- fixed MariaDB / InnoDB limits ----------------------------------------
MAX_IDENTIFIER = 64            # table, column and index names
MAX_COLUMNS_INNODB = 1017      # columns in one InnoDB table
MAX_INDEXES_INNODB = 64        # indexes on one InnoDB table
MAX_INDEX_COLUMNS = 16         # columns in one index
MAX_ROW_BYTES = 65535          # SQL-layer row size, all engines
MAX_VARCHAR_BYTES = 65532      # widest possible VARCHAR

# Index key bytes depend on the row format.
MAX_INDEX_KEY_MODERN = 3072    # DYNAMIC / COMPRESSED
MAX_INDEX_KEY_LEGACY = 767     # COMPACT / REDUNDANT

MODERN_ROW_FORMATS = {"Dynamic", "Compressed"}

# FULLTEXT and SPATIAL indexes are not subject to the B-tree key size limit.
UNCONSTRAINED_INDEX_TYPES = {"FULLTEXT", "SPATIAL"}

# Variable-length columns InnoDB may move off-page.
VARIABLE_TYPES = {
    "varchar", "varbinary", "text", "tinytext", "mediumtext", "longtext",
    "blob", "tinyblob", "mediumblob", "longblob", "json",
}

# An off-page column leaves only a 20-byte pointer in the row.
OFF_PAGE_POINTER = 20

# Identifier length is pass/fail rather than a gradual squeeze, so it is only
# worth reporting once a name is genuinely close to the ceiling.
IDENTIFIER_WARN_PCT = 90


def innodb_row_bytes(cols, modern: bool) -> int:
    """Best-case bytes InnoDB must keep in the row itself.

    On DYNAMIC/COMPRESSED any variable-length column can be pushed off-page,
    leaving a 20-byte pointer — which is why a 155-column table of VARCHAR(140)
    stores happily despite being far past the SQL-layer row size. On
    COMPACT/REDUNDANT the first 768 bytes stay inline, so the ceiling bites
    much sooner.
    """
    total = 0
    for c in cols:
        dtype = c.get("dtype")
        if dtype in VARIABLE_TYPES:
            if modern:
                total += OFF_PAGE_POINTER
            else:
                total += min(int(c.get("octet_len") or 0) + 2, 768)
        else:
            total += column_inline_bytes(c, modern)
    return total

# Largest value each integer type can reach, signed and unsigned.
INT_MAX = {
    "tinyint": (127, 255),
    "smallint": (32767, 65535),
    "mediumint": (8388607, 16777215),
    "int": (2147483647, 4294967295),
    "bigint": (9223372036854775807, 18446744073709551615),
}

# Fixed widths for building an index key estimate.
NUMERIC_KEY_BYTES = {
    "tinyint": 1, "smallint": 2, "mediumint": 3, "int": 4, "bigint": 8,
    "float": 4, "double": 8, "date": 3, "datetime": 8, "timestamp": 4,
    "time": 3, "year": 1, "decimal": 9,
}


def _server_limits() -> dict:
    """Resolve the limits that depend on server configuration."""
    variables = {}
    for name in (
        "innodb_page_size",
        "innodb_default_row_format",
        "innodb_strict_mode",
        "max_allowed_packet",
        "version",
    ):
        row = frappe.db.sql("SHOW VARIABLES LIKE %s", (name,))
        variables[name] = row[0][1] if row else ""

    page_size = int(variables.get("innodb_page_size") or 16384)

    # InnoDB must fit at least two rows in a page, so inline data per row is
    # capped well below the SQL-layer 65,535 bytes.
    max_inline = page_size // 2 - 65

    return {
        "page_size": page_size,
        "max_inline_row_bytes": max_inline,
        "default_row_format": variables.get("innodb_default_row_format", ""),
        "strict_mode": variables.get("innodb_strict_mode", ""),
        "max_allowed_packet": int(variables.get("max_allowed_packet") or 0),
        "version": variables.get("version", ""),
    }


def _severity(actual, limit, warn_pct):
    """Classify how close a value sits to its ceiling."""
    if not limit:
        return None, 0.0
    pct = round(actual * 100.0 / limit, 1)
    if actual > limit:
        return "violation", pct
    if pct >= 90:
        return "critical", pct
    if pct >= warn_pct:
        return "warning", pct
    return None, pct


def _key_bytes(col, sub_part) -> int:
    """Bytes this column contributes to an index key."""
    if sub_part:
        # SUB_PART is in characters; scale by the column's bytes-per-character.
        per_char = 4
        if col.get("maxlen"):
            per_char = max(int((col.get("octet_len") or 0) / col["maxlen"]), 1)
        return int(sub_part) * per_char
    if col.get("octet_len"):
        return int(col["octet_len"])
    return NUMERIC_KEY_BYTES.get(col.get("dtype"), 8)


def audit_limits(warn_pct: int = 70, include_ok: bool = False):
    require_mariadb()
    started = time.monotonic()

    server = _server_limits()
    db = db_name()

    tables = {
        r.name: r
        for r in frappe.db.sql(
            """
            SELECT TABLE_NAME AS name, ENGINE AS engine, ROW_FORMAT AS row_format,
                   AUTO_INCREMENT AS auto_increment, TABLE_ROWS AS n_rows
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
            """,
            (db,),
            as_dict=True,
        )
    }

    columns = frappe.db.sql(
        """
        SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col, DATA_TYPE AS dtype,
               COLUMN_TYPE AS ctype, CHARACTER_MAXIMUM_LENGTH AS maxlen,
               CHARACTER_OCTET_LENGTH AS octet_len, NUMERIC_PRECISION AS nprec,
               EXTRA AS extra
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        """,
        (db,),
        as_dict=True,
    )

    index_rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS tbl, INDEX_NAME AS idx, COLUMN_NAME AS col,
               SEQ_IN_INDEX AS seq, SUB_PART AS sub_part, INDEX_TYPE AS itype
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """,
        (db,),
        as_dict=True,
    )

    by_table = {}
    for c in columns:
        by_table.setdefault(c.tbl, []).append(c)

    indexes = {}
    for r in index_rows:
        indexes.setdefault(r.tbl, {}).setdefault(r.idx, []).append(r)

    findings = []

    def add(check, object_type, table, object_name, actual, limit, severity, pct, suggestion):
        findings.append(
            {
                "check": check,
                "object_type": object_type,
                "table_name": table,
                "doctype": doctype_of(table) if table.startswith("tab") else "",
                "object_name": object_name,
                "actual": actual,
                "limit": limit,
                "usage_pct": pct,
                "severity": severity,
                "suggestion": suggestion,
            }
        )

    for table, info in tables.items():
        cols = by_table.get(table, [])
        table_indexes = indexes.get(table, {})
        modern = (info.row_format or "") in MODERN_ROW_FORMATS
        max_key = MAX_INDEX_KEY_MODERN if modern else MAX_INDEX_KEY_LEGACY

        # 1. Identifier length --------------------------------------------
        sev, pct = _severity(len(table), MAX_IDENTIFIER, IDENTIFIER_WARN_PCT)
        if sev or include_ok:
            add(
                "Identifier length", "table", table, table, len(table), MAX_IDENTIFIER,
                sev or "ok", pct,
                "A DocType name may use at most 61 characters, since Frappe prefixes "
                "the table with `tab`. Rename the DocType to something shorter."
                if sev
                else "Within limit",
            )

        for c in cols:
            sev, pct = _severity(len(c.col), MAX_IDENTIFIER, IDENTIFIER_WARN_PCT)
            if sev:
                add(
                    "Identifier length", "column", table, c.col, len(c.col), MAX_IDENTIFIER,
                    sev, pct,
                    "Shorten the fieldname — MariaDB rejects identifiers longer than 64 characters.",
                )

            # 2. VARCHAR width --------------------------------------------
            if c.dtype == "varchar" and c.octet_len:
                sev, pct = _severity(int(c.octet_len), MAX_VARCHAR_BYTES, warn_pct)
                if sev:
                    add(
                        "VARCHAR width", "column", table, c.col, int(c.octet_len), MAX_VARCHAR_BYTES,
                        sev, pct,
                        "Convert the field to Text/Long Text — a VARCHAR this wide is "
                        "stored inline and eats the row budget.",
                    )

        # 3. Columns per table --------------------------------------------
        sev, pct = _severity(len(cols), MAX_COLUMNS_INNODB, warn_pct)
        if sev or include_ok:
            add(
                "Columns per table", "table", table, table, len(cols), MAX_COLUMNS_INNODB,
                sev or "ok", pct,
                "Move rarely used fields into a child table — InnoDB refuses more "
                f"than {MAX_COLUMNS_INNODB} columns."
                if sev
                else "Within limit",
            )

        # 4. Indexes per table --------------------------------------------
        sev, pct = _severity(len(table_indexes), MAX_INDEXES_INNODB, warn_pct)
        if sev or include_ok:
            add(
                "Indexes per table", "table", table, table, len(table_indexes), MAX_INDEXES_INNODB,
                sev or "ok", pct,
                "Drop redundant indexes — see the Duplicate Index Detector."
                if sev
                else "Within limit",
            )

        # 5. Index shape ---------------------------------------------------
        for idx_name, parts in table_indexes.items():
            sev, pct = _severity(len(parts), MAX_INDEX_COLUMNS, warn_pct)
            if sev:
                add(
                    "Columns per index", "index", table, idx_name, len(parts), MAX_INDEX_COLUMNS,
                    sev, pct,
                    f"An index may span at most {MAX_INDEX_COLUMNS} columns. Split it, "
                    "or drop the trailing columns that rarely filter.",
                )

            if parts[0].itype in UNCONSTRAINED_INDEX_TYPES:
                continue

            col_meta = {c.col: c for c in cols}
            key_bytes = sum(
                _key_bytes(col_meta.get(p.col, {}), p.sub_part) for p in parts
            )
            sev, pct = _severity(key_bytes, max_key, warn_pct)
            if sev:
                add(
                    "Index key bytes", "index", table, idx_name, key_bytes, max_key,
                    sev, pct,
                    f"Row format is {info.row_format or '?'} (limit {max_key} bytes). "
                    + (
                        "Index a prefix of the column instead, e.g. `col(191)`."
                        if modern
                        else "Switch the table to ROW_FORMAT=DYNAMIC to raise the limit to 3072 bytes."
                    ),
                )

        # 6. Row size ------------------------------------------------------
        total_inline = sum(column_inline_bytes(c, modern) for c in cols)

        sev, pct = _severity(total_inline, MAX_ROW_BYTES, warn_pct)
        if sev:
            add(
                "Row size (SQL limit)", "table", table, table, total_inline, MAX_ROW_BYTES,
                sev, pct,
                "Convert wide Data fields to Text, or move them to a child table.",
            )

        # InnoDB additionally has to fit two rows in a page. Variable-length
        # columns can go off-page, so only what must stay inline is counted.
        inline_only = innodb_row_bytes(cols, modern)
        sev, pct = _severity(inline_only, server["max_inline_row_bytes"], warn_pct)
        if sev:
            add(
                "Inline row size (InnoDB page)", "table", table, table,
                inline_only, server["max_inline_row_bytes"], sev, pct,
                f"InnoDB fits two rows per {server['page_size']}-byte page, so ALTER "
                "fails with “Row size too large” past this. "
                + (
                    "Move rarely used fields to a child table."
                    if modern
                    else "Switch the table to ROW_FORMAT=DYNAMIC so long values move off-page."
                ),
            )

        # 7. AUTO_INCREMENT headroom ---------------------------------------
        if info.auto_increment:
            auto_col = next((c for c in cols if "auto_increment" in (c.extra or "")), None)
            if auto_col:
                unsigned = "unsigned" in (auto_col.ctype or "").lower()
                bounds = INT_MAX.get(auto_col.dtype)
                if bounds:
                    ceiling = bounds[1] if unsigned else bounds[0]
                    sev, pct = _severity(int(info.auto_increment), ceiling, warn_pct)
                    if sev:
                        add(
                            "AUTO_INCREMENT headroom", "column", table, auto_col.col,
                            int(info.auto_increment), ceiling, sev, pct,
                            f"`{auto_col.col}` is {auto_col.ctype}. Widen it to BIGINT "
                            "before it runs out, or inserts will start failing.",
                        )

    order = {"violation": 0, "critical": 1, "warning": 2, "ok": 3}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), -float(f["usage_pct"] or 0)))

    by_check = {}
    by_severity = {"violation": 0, "critical": 0, "warning": 0, "ok": 0}
    for f in findings:
        by_check[f["check"]] = by_check.get(f["check"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    return {
        "findings": findings,
        "server": server,
        "grouped": {"by_check": by_check, "by_severity": by_severity},
        "summary": {
            "database": db,
            "server_version": server["version"],
            "page_size": server["page_size"],
            "default_row_format": server["default_row_format"],
            "strict_mode": server["strict_mode"],
            "max_inline_row_bytes": server["max_inline_row_bytes"],
            "max_allowed_packet": fmt_bytes(server["max_allowed_packet"]),
            "tables_scanned": len(tables),
            "columns_scanned": len(columns),
            "indexes_scanned": sum(len(v) for v in indexes.values()),
            "findings": len(findings),
            "violations": by_severity["violation"],
            "critical": by_severity["critical"],
            "warnings": by_severity["warning"],
            "warn_pct": warn_pct,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_limits_report(warn_pct: int = 70, include_ok: bool = False, fmt: str = "json"):
    guard()
    from db_tools.backend.common import as_bool

    payload = audit_limits(
        warn_pct=as_int(warn_pct, 70),
        include_ok=as_bool(include_ok),
    )
    return respond(payload, fmt, "findings", COLUMNS, "MariaDB Limits Audit")
