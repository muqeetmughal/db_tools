"""Detect child-table rows whose parent document no longer exists."""

import time

import frappe

from db_tools.backend.common import (
    as_bool,
    as_int,
    as_list,
    existing_tables,
    guard,
    quote,
    respond,
    table_columns,
)

COLUMNS = [
    "child_doctype",
    "child_table",
    "row_name",
    "parent",
    "parenttype",
    "parentfield",
    "reason",
    "severity",
]

# Frappe stores these parenttypes without a backing document table:
# ``__default`` / ``__global`` on tabDefaultValue hold site-wide defaults.
SKIP_PARENTTYPE_PREFIX = "__"


def _child_doctypes(only: list) -> list:
    filters = {"istable": 1}
    rows = frappe.get_all(
        "DocType",
        filters=filters,
        fields=["name", "module"],
        order_by="name asc",
    )
    if only:
        wanted = {d.lower() for d in only}
        rows = [r for r in rows if r.name.lower() in wanted]
    return rows


def detect_orphan_children(
    doctype: str | None = None,
    check_parentfield: bool = True,
    include_blank_parent: bool = True,
    limit: int | None = 5000,
):
    """Scan every child DocType and report rows without a live parent.

    Uses one ``LEFT JOIN`` per (child table, parenttype) pair — no per-row
    lookups.
    """
    started = time.monotonic()

    tables = existing_tables()
    all_doctypes = frappe.get_all("DocType", fields=["name", "issingle"])
    valid_doctypes = {d.name for d in all_doctypes}
    single_doctypes = {d.name for d in all_doctypes if d.issingle}

    findings = []
    truncated = False
    tables_scanned = 0
    rows_checked = 0
    pairs_scanned = 0

    for child in _child_doctypes(as_list(doctype)):
        table = f"tab{child.name}"
        if table not in tables:
            continue

        cols = table_columns(table)
        if not {"name", "parent", "parenttype"} <= cols:
            continue

        tables_scanned += 1
        has_parentfield = "parentfield" in cols

        total = frappe.db.sql(f"SELECT COUNT(*) FROM {quote(table)}")[0][0]
        rows_checked += total or 0

        if include_blank_parent:
            blanks = frappe.db.sql(
                f"""
                SELECT `name`, `parent`, `parenttype`
                    {", `parentfield`" if has_parentfield else ""}
                FROM {quote(table)}
                WHERE `parent` IS NULL OR TRIM(`parent`) = ''
                   OR `parenttype` IS NULL OR TRIM(`parenttype`) = ''
                LIMIT %s
                """,
                (_room(limit, findings),),
                as_dict=True,
            )
            for row in blanks:
                findings.append(
                    {
                        "child_doctype": child.name,
                        "child_table": table,
                        "row_name": row.name,
                        "parent": row.parent or "",
                        "parenttype": row.parenttype or "",
                        "parentfield": (row.get("parentfield") or "") if has_parentfield else "",
                        "reason": "Row has no parent / parenttype set",
                        "severity": "warning",
                    }
                )
            if _full(limit, findings):
                truncated = True
                break

        parenttypes = frappe.db.sql(
            f"""
            SELECT `parenttype` AS pt, COUNT(*) AS n
            FROM {quote(table)}
            WHERE `parenttype` IS NOT NULL AND TRIM(`parenttype`) != ''
            GROUP BY `parenttype`
            """,
            as_dict=True,
        )

        for entry in parenttypes:
            parenttype = entry.pt
            parent_table = f"tab{parenttype}"

            # Frappe's own internal buckets (``__default``, ``__global``) are
            # not documents and have no parent to resolve.
            if parenttype.startswith(SKIP_PARENTTYPE_PREFIX):
                continue

            pairs_scanned += 1

            # A Single DocType keeps its values in `tabSingles`; its child rows
            # legitimately carry parent == parenttype and no parent table.
            if parenttype in single_doctypes:
                rows = frappe.db.sql(
                    f"""
                    SELECT `name`, `parent`, `parenttype`
                        {", `parentfield`" if has_parentfield else ""}
                    FROM {quote(table)}
                    WHERE `parenttype` = %s AND `parent` != %s
                    LIMIT %s
                    """,
                    (parenttype, parenttype, _room(limit, findings)),
                    as_dict=True,
                )
                for row in rows:
                    findings.append(
                        {
                            "child_doctype": child.name,
                            "child_table": table,
                            "row_name": row.name,
                            "parent": row.parent,
                            "parenttype": parenttype,
                            "parentfield": (row.get("parentfield") or "") if has_parentfield else "",
                            "reason": (
                                f"{parenttype} is a Single DocType, so parent should be "
                                f"'{parenttype}' — found '{row.parent}'"
                            ),
                            "severity": "warning",
                        }
                    )
            elif parenttype not in valid_doctypes:
                rows = frappe.db.sql(
                    f"""
                    SELECT `name`, `parent`, `parenttype`
                        {", `parentfield`" if has_parentfield else ""}
                    FROM {quote(table)}
                    WHERE `parenttype` = %s
                    LIMIT %s
                    """,
                    (parenttype, _room(limit, findings)),
                    as_dict=True,
                )
                for row in rows:
                    findings.append(
                        {
                            "child_doctype": child.name,
                            "child_table": table,
                            "row_name": row.name,
                            "parent": row.parent,
                            "parenttype": parenttype,
                            "parentfield": (row.get("parentfield") or "") if has_parentfield else "",
                            "reason": f"parenttype '{parenttype}' is not an installed DocType",
                            "severity": "critical",
                        }
                    )
            elif parent_table not in tables:
                rows = frappe.db.sql(
                    f"""
                    SELECT `name`, `parent`, `parenttype`
                        {", `parentfield`" if has_parentfield else ""}
                    FROM {quote(table)}
                    WHERE `parenttype` = %s
                    LIMIT %s
                    """,
                    (parenttype, _room(limit, findings)),
                    as_dict=True,
                )
                for row in rows:
                    findings.append(
                        {
                            "child_doctype": child.name,
                            "child_table": table,
                            "row_name": row.name,
                            "parent": row.parent,
                            "parenttype": parenttype,
                            "parentfield": (row.get("parentfield") or "") if has_parentfield else "",
                            "reason": f"Table `{parent_table}` does not exist",
                            "severity": "critical",
                        }
                    )
            else:
                rows = frappe.db.sql(
                    f"""
                    SELECT c.`name` AS name, c.`parent` AS parent, c.`parenttype` AS parenttype
                        {", c.`parentfield` AS parentfield" if has_parentfield else ""}
                    FROM {quote(table)} c
                    LEFT JOIN {quote(parent_table)} p ON p.`name` = c.`parent`
                    WHERE c.`parenttype` = %s
                      AND c.`parent` IS NOT NULL AND TRIM(c.`parent`) != ''
                      AND p.`name` IS NULL
                    LIMIT %s
                    """,
                    (parenttype, _room(limit, findings)),
                    as_dict=True,
                )
                for row in rows:
                    findings.append(
                        {
                            "child_doctype": child.name,
                            "child_table": table,
                            "row_name": row.name,
                            "parent": row.parent,
                            "parenttype": parenttype,
                            "parentfield": (row.get("parentfield") or "") if has_parentfield else "",
                            "reason": f"Parent {parenttype} '{row.parent}' no longer exists",
                            "severity": "critical",
                        }
                    )

            if _full(limit, findings):
                truncated = True
                break

        if truncated:
            break

        if check_parentfield and has_parentfield:
            findings.extend(_bad_parentfields(child.name, table, valid_doctypes, limit, findings))
            if _full(limit, findings):
                truncated = True
                break

    grouped = {}
    by_severity = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        grouped[f["child_doctype"]] = grouped.get(f["child_doctype"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    return {
        "findings": findings,
        "grouped": {"by_child_doctype": grouped, "by_severity": by_severity},
        "summary": {
            "child_doctypes_scanned": tables_scanned,
            "parent_links_scanned": pairs_scanned,
            "rows_checked": rows_checked,
            "orphan_rows": len(findings),
            "affected_doctypes": len(grouped),
            "truncated": truncated,
            "execution_time": round(time.monotonic() - started, 3),
        },
    }


def _bad_parentfields(child_doctype, table, valid_doctypes, limit, findings):
    """Rows whose parentfield does not exist on the parent DocType."""
    out = []
    combos = frappe.db.sql(
        f"""
        SELECT `parenttype` AS pt, `parentfield` AS pf, COUNT(*) AS n
        FROM {quote(table)}
        WHERE `parenttype` IS NOT NULL AND TRIM(`parenttype`) != ''
        GROUP BY `parenttype`, `parentfield`
        """,
        as_dict=True,
    )

    for combo in combos:
        if combo.pt not in valid_doctypes or combo.pt.startswith(SKIP_PARENTTYPE_PREFIX):
            continue
        if not combo.pf:
            continue
        try:
            meta = frappe.get_meta(combo.pt)
        except Exception:
            continue
        if meta.get_field(combo.pf):
            continue

        rows = frappe.db.sql(
            f"""
            SELECT `name`, `parent`, `parenttype`, `parentfield`
            FROM {quote(table)}
            WHERE `parenttype` = %s AND `parentfield` = %s
            LIMIT %s
            """,
            (combo.pt, combo.pf, _room(limit, findings + out)),
            as_dict=True,
        )
        for row in rows:
            out.append(
                {
                    "child_doctype": child_doctype,
                    "child_table": table,
                    "row_name": row.name,
                    "parent": row.parent,
                    "parenttype": row.parenttype,
                    "parentfield": row.parentfield,
                    "reason": f"parentfield '{combo.pf}' no longer exists on {combo.pt}",
                    "severity": "warning",
                }
            )
    return out


def _room(limit, findings):
    """How many more rows we may still collect (SQL LIMIT)."""
    if not limit:
        return 100000
    return max(limit - len(findings), 0) or 1


def _full(limit, findings):
    return bool(limit) and len(findings) >= limit


@frappe.whitelist()
def get_orphan_children_report(
    doctype: str | None = None,
    check_parentfield: bool = True,
    include_blank_parent: bool = True,
    limit: int | None = 5000,
    fmt: str = "json",
):
    guard()
    payload = detect_orphan_children(
        doctype=doctype or None,
        check_parentfield=as_bool(check_parentfield),
        include_blank_parent=as_bool(include_blank_parent),
        limit=as_int(limit, 5000) or None,
    )
    return respond(payload, fmt, "findings", COLUMNS, "Orphan Child Rows")
