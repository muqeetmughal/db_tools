"""Find fields missing a database column, and columns whose type drifted."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
    as_list,
    db_name,
    guard,
    require_mariadb,
    respond,
)

COLUMNS = [
    "doctype",
    "table_name",
    "column_name",
    "fieldtype",
    "kind",
    "expected",
    "actual",
    "severity",
    "fix_sql",
]

# Frappe creates and maintains these itself regardless of what a DocType
# declares, so a DocField of the same name is not real drift.
FRAMEWORK_COLUMNS = {"_user_tags", "_comments", "_assign", "_liked_by", "_seen"}


def _db_columns() -> dict:
    rows = frappe.db.sql(
        """
        SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col, DATA_TYPE AS dtype,
               COLUMN_TYPE AS ctype, CHARACTER_MAXIMUM_LENGTH AS maxlen,
               NUMERIC_PRECISION AS nprec, NUMERIC_SCALE AS nscale
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        """,
        (db_name(),),
        as_dict=True,
    )
    out = {}
    for r in rows:
        out.setdefault(r.tbl, {})[r.col] = r
    return out


def _expected_definition(df) -> str | None:
    """The exact column type Frappe would create for this field.

    Delegates to ``frappe.database.schema.get_definition`` so per-field
    ``length`` and ``precision`` overrides (varchar(240), decimal(21,2)) are
    honoured instead of assuming the type_map default.
    """
    from frappe.database.schema import get_definition

    try:
        return get_definition(
            df.fieldtype,
            precision=df.get("precision"),
            length=df.get("length"),
            options=df.get("options"),
        )
    except Exception:
        return None


def _split(definition: str):
    """'varchar(140)' -> ('varchar', '140'); 'longtext' -> ('longtext', '')."""
    text = (definition or "").strip().lower()
    if "(" in text:
        base, _, rest = text.partition("(")
        return base.strip(), rest.rstrip(")").strip()
    return text, ""


def detect_schema_drift(
    doctype: str | None = None,
    check_types: bool = True,
    check_lengths: bool = True,
    include_custom_fields: bool = True,
):
    require_mariadb()
    started = time.monotonic()

    from frappe.model import no_value_fields

    db_cols = _db_columns()
    only = {d.lower() for d in as_list(doctype)}

    doctypes = frappe.get_all(
        "DocType",
        filters={"issingle": 0, "is_virtual": 0},
        fields=["name", "module"],
        order_by="name asc",
    )

    findings = []
    doctypes_scanned = 0
    fields_checked = 0

    for dt in doctypes:
        if only and dt.name.lower() not in only:
            continue

        table = f"tab{dt.name}"
        cols = db_cols.get(table)
        if cols is None:
            # A DocType with no table at all is the Orphan Table Finder's job.
            continue

        try:
            meta = frappe.get_meta(dt.name)
        except Exception:
            continue

        doctypes_scanned += 1

        for df in meta.fields:
            if df.fieldtype in no_value_fields or not df.fieldname:
                continue

            if df.fieldname in FRAMEWORK_COLUMNS:
                continue

            expected = _expected_definition(df)
            if not expected:
                continue

            is_custom = bool(df.get("is_custom_field"))
            if is_custom and not include_custom_fields:
                continue

            fields_checked += 1
            exp_base, exp_args = _split(expected)
            col = cols.get(df.fieldname)

            if col is None:
                findings.append(
                    {
                        "doctype": dt.name,
                        "module": dt.module or "",
                        "table_name": table,
                        "column_name": df.fieldname,
                        "fieldtype": df.fieldtype,
                        "kind": "missing column",
                        "expected": expected,
                        "actual": "—",
                        "severity": "critical",
                        "is_custom": is_custom,
                        "fix_sql": (
                            f"ALTER TABLE `{table}` ADD COLUMN `{df.fieldname}` {expected};"
                        ),
                    }
                )
                continue

            act_base, act_args = _split(col.ctype)

            if act_base == exp_base and act_args == exp_args:
                continue

            if act_base != exp_base:
                if not check_types:
                    continue
                kind, severity = "type mismatch", "warning"
            else:
                if not check_lengths:
                    continue
                kind, severity = "length mismatch", "info"

            findings.append(
                {
                    "doctype": dt.name,
                    "module": dt.module or "",
                    "table_name": table,
                    "column_name": df.fieldname,
                    "fieldtype": df.fieldtype,
                    "kind": kind,
                    "expected": expected,
                    "actual": col.ctype,
                    "severity": severity,
                    "is_custom": is_custom,
                    "fix_sql": f"ALTER TABLE `{table}` MODIFY `{df.fieldname}` {expected};",
                }
            )

    by_kind = {}
    by_severity = {"critical": 0, "warning": 0, "info": 0}
    by_doctype = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        by_doctype[f["doctype"]] = by_doctype.get(f["doctype"], 0) + 1

    return {
        "findings": findings,
        "grouped": {"by_kind": by_kind, "by_severity": by_severity, "by_doctype": by_doctype},
        "summary": {
            "doctypes_scanned": doctypes_scanned,
            "fields_checked": fields_checked,
            "drifted": len(findings),
            "missing_columns": by_kind.get("missing column", 0),
            "type_mismatches": by_kind.get("type mismatch", 0),
            "length_mismatches": by_kind.get("length mismatch", 0),
            "affected_doctypes": len(by_doctype),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_schema_drift_report(
    doctype: str | None = None,
    check_types: bool = True,
    check_lengths: bool = True,
    include_custom_fields: bool = True,
    fmt: str = "json",
):
    guard()
    payload = detect_schema_drift(
        doctype=doctype or None,
        check_types=as_bool(check_types),
        check_lengths=as_bool(check_lengths),
        include_custom_fields=as_bool(include_custom_fields),
    )
    return respond(payload, fmt, "findings", COLUMNS, "Schema Drift")
