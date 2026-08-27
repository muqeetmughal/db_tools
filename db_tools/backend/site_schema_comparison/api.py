"""Compare the schema of two sites or environments side by side."""

import json
import os
import time
from contextlib import closing, contextmanager

import frappe

from db_tools.backend.common import as_bool, doctype_of, guard, require_mariadb, respond

COLUMNS = ["table_name", "doctype", "column_name", "kind", "site_a", "site_b", "severity"]


# --------------------------------------------------------------------------
# Site discovery
# --------------------------------------------------------------------------


def _sites_path() -> str:
    return os.path.join(frappe.utils.get_bench_path(), "sites")


def list_bench_sites() -> list:
    """Every directory under ``sites/`` that carries a site_config.json."""
    root = _sites_path()
    out = []

    if not os.path.isdir(root):
        return out

    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry, "site_config.json")
        if os.path.isfile(path):
            out.append(entry)
    return out


def _site_db_config(site: str) -> dict:
    """Read a site's database credentials off disk.

    Returned to callers inside this module only — never to the browser.
    """
    if site not in list_bench_sites():
        frappe.throw(f"Unknown site: {site}")

    with open(os.path.join(_sites_path(), site, "site_config.json"), encoding="utf-8") as f:
        conf = json.load(f)

    common_path = os.path.join(_sites_path(), "common_site_config.json")
    common = {}
    if os.path.isfile(common_path):
        with open(common_path, encoding="utf-8") as f:
            common = json.load(f)

    db_name = conf.get("db_name")
    if not db_name:
        frappe.throw(f"Site '{site}' has no db_name in its site_config.json")

    return {
        "host": conf.get("db_host") or common.get("db_host") or "127.0.0.1",
        "port": int(conf.get("db_port") or common.get("db_port") or 3306),
        "user": conf.get("db_user") or db_name,
        "password": conf.get("db_password") or "",
        "db_name": db_name,
        "db_type": conf.get("db_type") or common.get("db_type") or "mariadb",
    }


@contextmanager
def _connect(site: str):
    """Short-lived read-only connection to a site's database."""
    import pymysql

    cfg = _site_db_config(site)
    if cfg["db_type"] not in ("mariadb", "mysql"):
        frappe.throw(f"Site '{site}' runs on {cfg['db_type']}; this tool supports MariaDB/MySQL only.")

    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["db_name"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )
    try:
        yield conn, cfg["db_name"]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Schema reading
# --------------------------------------------------------------------------

_COLUMN_SQL = """
    SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col, COLUMN_TYPE AS coltype,
           IS_NULLABLE AS nullable, COLUMN_DEFAULT AS coldefault
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = %s
"""

_INDEX_SQL = """
    SELECT TABLE_NAME AS tbl, INDEX_NAME AS idx, COLUMN_NAME AS col, SEQ_IN_INDEX AS seq
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = %s
    ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
"""


def _shape(column_rows, index_rows) -> dict:
    """Fold raw rows into {table: {"columns": {name: spec}, "indexes": {name: [cols]}}}."""
    schema = {}

    for r in column_rows:
        table = schema.setdefault(r["tbl"], {"columns": {}, "indexes": {}})
        table["columns"][r["col"]] = {
            "type": r["coltype"],
            "nullable": r["nullable"],
            "default": r["coldefault"],
        }

    for r in index_rows:
        table = schema.setdefault(r["tbl"], {"columns": {}, "indexes": {}})
        table["indexes"].setdefault(r["idx"], []).append(r["col"])

    return schema


def _read_schema(site: str, current_site: str) -> dict:
    """Read a site's schema — reusing the live connection for the current site."""
    if site == current_site:
        db = frappe.conf.db_name
        cols = frappe.db.sql(_COLUMN_SQL, (db,), as_dict=True)
        idxs = frappe.db.sql(_INDEX_SQL, (db,), as_dict=True)
        return _shape(cols, idxs)

    with _connect(site) as (conn, db):
        with closing(conn.cursor()) as cur:
            cur.execute(_COLUMN_SQL, (db,))
            cols = cur.fetchall()
            cur.execute(_INDEX_SQL, (db,))
            idxs = cur.fetchall()
    return _shape(cols, idxs)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def compare_sites(
    site_a: str,
    site_b: str,
    compare_columns: bool = True,
    compare_indexes: bool = True,
    only_doctype_tables: bool = True,
):
    require_mariadb()
    started = time.monotonic()

    if not site_a or not site_b:
        frappe.throw("Pick two sites to compare.")
    if site_a == site_b:
        frappe.throw("Pick two different sites.")

    current = frappe.local.site
    schema_a = _read_schema(site_a, current)
    schema_b = _read_schema(site_b, current)

    if only_doctype_tables:
        schema_a = {k: v for k, v in schema_a.items() if k.startswith("tab")}
        schema_b = {k: v for k, v in schema_b.items() if k.startswith("tab")}

    tables_a, tables_b = set(schema_a), set(schema_b)
    diffs = []

    def add(table, column, kind, a_val, b_val, severity):
        diffs.append(
            {
                "table_name": table,
                "doctype": doctype_of(table) if table.startswith("tab") else "",
                "column_name": column,
                "kind": kind,
                "site_a": a_val,
                "site_b": b_val,
                "severity": severity,
            }
        )

    for table in sorted(tables_a - tables_b):
        add(table, "", "table missing on B", f"{len(schema_a[table]['columns'])} columns", "—", "critical")

    for table in sorted(tables_b - tables_a):
        add(table, "", "table missing on A", "—", f"{len(schema_b[table]['columns'])} columns", "critical")

    common = sorted(tables_a & tables_b)

    for table in common:
        cols_a = schema_a[table]["columns"]
        cols_b = schema_b[table]["columns"]

        if compare_columns:
            for col in sorted(set(cols_a) - set(cols_b)):
                add(table, col, "column missing on B", cols_a[col]["type"], "—", "warning")
            for col in sorted(set(cols_b) - set(cols_a)):
                add(table, col, "column missing on A", "—", cols_b[col]["type"], "warning")

            for col in sorted(set(cols_a) & set(cols_b)):
                a, b = cols_a[col], cols_b[col]
                if a["type"] != b["type"]:
                    add(table, col, "type mismatch", a["type"], b["type"], "warning")
                elif a["nullable"] != b["nullable"]:
                    add(
                        table,
                        col,
                        "nullability mismatch",
                        f"NULL={a['nullable']}",
                        f"NULL={b['nullable']}",
                        "info",
                    )

        if compare_indexes:
            idx_a = schema_a[table]["indexes"]
            idx_b = schema_b[table]["indexes"]
            for name in sorted(set(idx_a) - set(idx_b)):
                add(table, name, "index missing on B", ", ".join(idx_a[name]), "—", "info")
            for name in sorted(set(idx_b) - set(idx_a)):
                add(table, name, "index missing on A", "—", ", ".join(idx_b[name]), "info")

    by_kind = {}
    by_severity = {"critical": 0, "warning": 0, "info": 0}
    by_table = {}
    for d in diffs:
        by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
        by_severity[d["severity"]] = by_severity.get(d["severity"], 0) + 1
        by_table[d["table_name"]] = by_table.get(d["table_name"], 0) + 1

    return {
        "differences": diffs,
        "grouped": {"by_kind": by_kind, "by_severity": by_severity, "by_table": by_table},
        "summary": {
            "site_a": site_a,
            "site_b": site_b,
            "tables_a": len(tables_a),
            "tables_b": len(tables_b),
            "tables_common": len(common),
            "tables_only_a": len(tables_a - tables_b),
            "tables_only_b": len(tables_b - tables_a),
            "differences": len(diffs),
            "affected_tables": len(by_table),
            "in_sync": not diffs,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_sites():
    """Sites on this bench, so the UI can offer a dropdown."""
    guard()
    return {"sites": list_bench_sites(), "current": frappe.local.site}


@frappe.whitelist()
def get_site_schema_comparison(
    site_a: str | None = None,
    site_b: str | None = None,
    compare_columns: bool = True,
    compare_indexes: bool = True,
    only_doctype_tables: bool = True,
    fmt: str = "json",
):
    guard()
    payload = compare_sites(
        site_a=site_a or frappe.local.site,
        site_b=site_b,
        compare_columns=as_bool(compare_columns),
        compare_indexes=as_bool(compare_indexes),
        only_doctype_tables=as_bool(only_doctype_tables),
    )
    return respond(payload, fmt, "differences", COLUMNS, "Site Schema Comparison")
