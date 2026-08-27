"""Audit Custom Fields and Property Setters for dangling references."""

import time

import frappe

from db_tools.backend.common import as_bool, db_name, guard, require_mariadb, respond

COLUMNS = ["source", "record", "doctype", "fieldname", "issue", "detail", "severity"]

# Link/Dynamic Link "options" that are not a DocType name.
NON_DOCTYPE_OPTIONS = {"", "[Select]"}


def _db_columns() -> dict:
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


def _add(findings, source, record, doctype, fieldname, issue, detail, severity):
    findings.append(
        {
            "source": source,
            "record": record,
            "doctype": doctype,
            "fieldname": fieldname or "",
            "issue": issue,
            "detail": detail,
            "severity": severity,
        }
    )


def _audit_custom_fields(findings, doctypes, tabled, db_cols, check_columns):
    from frappe.model import no_value_fields

    fields = frappe.get_all(
        "Custom Field",
        fields=["name", "dt", "fieldname", "fieldtype", "options", "insert_after"],
        limit=0,
    )

    seen = {}
    for cf in fields:
        # The DocType it customises must still exist.
        if cf.dt not in doctypes:
            _add(
                findings, "Custom Field", cf.name, cf.dt, cf.fieldname,
                "DocType missing",
                f"Custom Field targets '{cf.dt}', which is not an installed DocType",
                "critical",
            )
            continue

        # Duplicate (dt, fieldname) — Frappe will only honour one of them.
        key = (cf.dt, cf.fieldname)
        if key in seen:
            _add(
                findings, "Custom Field", cf.name, cf.dt, cf.fieldname,
                "duplicate field",
                f"Another Custom Field ('{seen[key]}') already defines {cf.fieldname} on {cf.dt}",
                "critical",
            )
        else:
            seen[key] = cf.name

        # A Link field pointing at a DocType that no longer exists.
        if cf.fieldtype in ("Link", "Table", "Table MultiSelect"):
            target = (cf.options or "").strip()
            if target not in NON_DOCTYPE_OPTIONS and target not in doctypes:
                _add(
                    findings, "Custom Field", cf.name, cf.dt, cf.fieldname,
                    "link target missing",
                    f"{cf.fieldtype} field points at '{target}', which is not an installed DocType",
                    "critical",
                )

        # The column should exist unless the fieldtype never creates one.
        if check_columns and cf.fieldtype not in no_value_fields and cf.dt in tabled:
            table = f"tab{cf.dt}"
            cols = db_cols.get(table)
            if cols is not None and cf.fieldname and cf.fieldname not in cols:
                _add(
                    findings, "Custom Field", cf.name, cf.dt, cf.fieldname,
                    "column missing",
                    f"No `{cf.fieldname}` column on `{table}` — run `bench migrate`",
                    "warning",
                )

        # insert_after should name a field that exists on the DocType.
        if cf.insert_after:
            try:
                meta = frappe.get_meta(cf.dt)
            except Exception:
                meta = None
            if meta and not meta.get_field(cf.insert_after):
                _add(
                    findings, "Custom Field", cf.name, cf.dt, cf.fieldname,
                    "insert_after dangling",
                    f"insert_after references '{cf.insert_after}', which is not a field on {cf.dt}",
                    "info",
                )

    return len(fields)


def _audit_property_setters(findings, doctypes):
    setters = frappe.get_all(
        "Property Setter",
        fields=["name", "doc_type", "field_name", "property", "doctype_or_field"],
        limit=0,
    )

    meta_cache = {}
    for ps in setters:
        if ps.doc_type not in doctypes:
            _add(
                findings, "Property Setter", ps.name, ps.doc_type, ps.field_name,
                "DocType missing",
                f"Property Setter targets '{ps.doc_type}', which is not an installed DocType",
                "critical",
            )
            continue

        if ps.doctype_or_field == "DocField" and ps.field_name:
            if ps.doc_type not in meta_cache:
                try:
                    meta_cache[ps.doc_type] = frappe.get_meta(ps.doc_type)
                except Exception:
                    meta_cache[ps.doc_type] = None
            meta = meta_cache[ps.doc_type]
            if meta and not meta.get_field(ps.field_name):
                _add(
                    findings, "Property Setter", ps.name, ps.doc_type, ps.field_name,
                    "field missing",
                    f"Sets '{ps.property}' on '{ps.field_name}', which is not a field on {ps.doc_type}",
                    "warning",
                )

    return len(setters)


def _audit_scripts(findings, doctypes):
    count = 0
    for doctype, field in (("Client Script", "dt"), ("Server Script", "reference_doctype")):
        if not frappe.db.exists("DocType", doctype):
            continue
        rows = frappe.get_all(doctype, fields=["name", field], limit=0)
        count += len(rows)
        for row in rows:
            target = row.get(field)
            if target and target not in doctypes:
                _add(
                    findings, doctype, row.name, target, "",
                    "DocType missing",
                    f"{doctype} targets '{target}', which is not an installed DocType",
                    "warning",
                )
    return count


def audit_customizations(
    check_columns: bool = True,
    check_property_setters: bool = True,
    check_scripts: bool = True,
):
    require_mariadb()
    started = time.monotonic()

    all_doctypes = frappe.get_all("DocType", fields=["name", "issingle", "is_virtual"])
    doctypes = {d.name for d in all_doctypes}
    tabled = {d.name for d in all_doctypes if not d.issingle and not d.is_virtual}
    db_cols = _db_columns() if check_columns else {}

    findings = []
    custom_fields = _audit_custom_fields(findings, doctypes, tabled, db_cols, check_columns)
    property_setters = _audit_property_setters(findings, doctypes) if check_property_setters else 0
    scripts = _audit_scripts(findings, doctypes) if check_scripts else 0

    by_issue = {}
    by_severity = {"critical": 0, "warning": 0, "info": 0}
    by_source = {}
    for f in findings:
        by_issue[f["issue"]] = by_issue.get(f["issue"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        by_source[f["source"]] = by_source.get(f["source"], 0) + 1

    return {
        "findings": findings,
        "grouped": {"by_issue": by_issue, "by_severity": by_severity, "by_source": by_source},
        "summary": {
            "custom_fields_checked": custom_fields,
            "property_setters_checked": property_setters,
            "scripts_checked": scripts,
            "issues": len(findings),
            "critical": by_severity["critical"],
            "warnings": by_severity["warning"],
            "affected_sources": len(by_source),
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


@frappe.whitelist()
def get_customization_report(
    check_columns: bool = True,
    check_property_setters: bool = True,
    check_scripts: bool = True,
    fmt: str = "json",
):
    guard()
    payload = audit_customizations(
        check_columns=as_bool(check_columns),
        check_property_setters=as_bool(check_property_setters),
        check_scripts=as_bool(check_scripts),
    )
    return respond(payload, fmt, "findings", COLUMNS, "Customization Audit")
