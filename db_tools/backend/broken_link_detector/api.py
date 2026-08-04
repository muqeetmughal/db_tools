"""Whitelisted endpoints for the Broken Link Detector frontend."""

import frappe

from db_tools.backend.broken_link_detector.broken_links import detect_broken_links
from db_tools.backend.broken_link_detector.utils import as_bool


@frappe.whitelist(allow_guest=True)
def get_broken_links_report(
    doctype: str | None = None,
    include_dynamic: bool = True,
    include_child_tables: bool = True,
    severity: str | None = None,
    limit: int | None = 5000,
    batch_size: int = 1000,
    fmt: str = "json",
):
    """Scan the site for broken links.

    ``fmt`` may be ``json`` (default structured payload), ``csv``, ``markdown``
    or ``console`` — non-json formats return ``{"data": "<rendered>"}`` for
    direct download.
    """
    report = detect_broken_links(
        doctype=doctype or None,
        include_dynamic=as_bool(include_dynamic),
        include_child_tables=as_bool(include_child_tables),
        severity=severity or None,
        limit=int(limit) if limit else None,
        batch_size=int(batch_size) or 1000,
    )
    if fmt and fmt != "json":
        from db_tools.backend.broken_link_detector.report import render

        return {"data": render(report, fmt), "format": fmt}
    return report.to_dict()
