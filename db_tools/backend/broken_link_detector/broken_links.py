"""Public API for the Broken Link Detector.

Example
-------
    report = detect_broken_links(doctype=None, include_dynamic=True,
                                 include_child_tables=True, severity=None)
    print(report.summary)
"""

from time import time

from db_tools.backend.broken_link_detector.models import Report, Summary
from db_tools.backend.broken_link_detector.scanner import discover_link_fields, get_doctypes
from db_tools.backend.broken_link_detector.validator import Validator


def detect_broken_links(
    doctype: str | list[str] | None = None,
    include_dynamic: bool = True,
    include_child_tables: bool = True,
    severity: str | None = None,
    limit: int | None = None,
    batch_size: int = 1000,
    severity_map: dict | None = None,
) -> Report:
    """Scan the site and return a structured :class:`Report`.

    Parameters
    ----------
    doctype: Restrict the scan to one or more DocTypes.
    include_dynamic: Validate Dynamic Link fields too.
    include_child_tables: Scan link fields inside child tables.
    severity: If given, only keep findings of this severity in the report.
    limit: Cap the number of findings collected (counts stay exact for Link fields).
    batch_size: Rows fetched per keyset-paginated batch.
    severity_map: DocType -> severity overrides for classification.

    This function is strictly read-only.
    """
    start = time()

    doctypes = get_doctypes(doctype, include_child_tables=include_child_tables)
    link_fields = discover_link_fields(doctypes, include_dynamic=include_dynamic)

    validator = Validator(batch_size=batch_size, limit=limit, severity_map=severity_map)

    for lf in link_fields:
        if lf.is_dynamic:
            validator.validate_dynamic_field(lf)
        else:
            validator.validate_link_field(lf)

    findings = validator.findings
    if severity:
        findings = [f for f in findings if f.severity == severity]

    summary = Summary(
        doctypes_scanned=len(doctypes),
        link_fields_scanned=sum(1 for f in link_fields if not f.is_dynamic),
        dynamic_link_fields_scanned=sum(1 for f in link_fields if f.is_dynamic),
        records_checked=validator.records_checked,
        broken_links=validator.broken_total,
        config_issues=len(validator.config_issues),
        execution_time=time() - start,
    )

    return Report(summary=summary, findings=findings, config_issues=validator.config_issues)
