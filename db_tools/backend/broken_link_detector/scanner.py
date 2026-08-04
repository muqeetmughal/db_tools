"""Metadata discovery for the Broken Link Detector.

Loads Link / Dynamic Link fields (standard + custom, including those inside
child tables) from Frappe's metadata APIs — never from hardcoded definitions.
"""

import frappe

from db_tools.backend.broken_link_detector.models import LinkField


def get_doctypes(doctype: str | list[str] | None = None, include_child_tables: bool = True) -> list[str]:
    """Return the list of DocTypes to scan.

    - ``doctype``: restrict to one or more specific DocTypes.
    - ``include_child_tables``: when False, excludes ``istable`` DocTypes.
    - Virtual DocTypes are always excluded (they have no table).
    """
    if doctype:
        if isinstance(doctype, str):
            doctype = [doctype]
        return [d for d in doctype if d]

    filters = {"is_virtual": 0}
    if not include_child_tables:
        filters["istable"] = 0
    return frappe.get_all("DocType", filters=filters, pluck="name")


def discover_link_fields(
    doctypes: list[str],
    include_dynamic: bool = True,
) -> list[LinkField]:
    """Discover every Link and Dynamic Link field for the given DocTypes."""
    link_fields: list[LinkField] = []

    for doctype in doctypes:
        if not frappe.db.table_exists(doctype):
            continue

        meta = frappe.get_meta(doctype)
        is_child = bool(getattr(meta, "istable", False))

        for df in meta.get_link_fields():
            fieldname = getattr(df, "fieldname", "")
            options = getattr(df, "options", "")
            if not fieldname or not options:
                continue
            link_fields.append(
                LinkField(
                    doctype=doctype,
                    fieldname=fieldname,
                    target_doctype=options,
                    fieldtype="Link",
                    is_custom=bool(getattr(df, "is_custom_field", False)),
                    source_is_child=is_child,
                )
            )

        if include_dynamic:
            for df in meta.get_dynamic_link_fields():
                fieldname = getattr(df, "fieldname", "")
                options = getattr(df, "options", "")
                if not fieldname or not options:
                    continue
                link_fields.append(
                    LinkField(
                        doctype=doctype,
                        fieldname=fieldname,
                        target_doctype="",
                        fieldtype="Dynamic Link",
                        doctype_field=options,
                        is_custom=bool(getattr(df, "is_custom_field", False)),
                        source_is_child=is_child,
                    )
                )

    return link_fields
