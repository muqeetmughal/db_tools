"""Validation engine for the Broken Link Detector.

Streams results in batches using JOIN-based queries. Never runs a query per
document and never modifies the database.
"""

import frappe

from db_tools.backend.broken_link_detector import queries
from db_tools.backend.broken_link_detector.models import ConfigIssue, Finding, LinkField
from db_tools.backend.broken_link_detector.utils import classify_severity

REASON_TARGET_NOT_FOUND = "Target document not found"


def get_target_status(target_doctype: str) -> str:
    """Classify a target DocType: ``ok``, ``virtual``, ``missing`` or ``missing_table``."""
    if not target_doctype:
        return "missing"

    try:
        if not frappe.db.exists("DocType", target_doctype):
            return "missing"
        if frappe.get_meta(target_doctype).is_virtual:
            return "virtual"
    except Exception:
        return "missing"

    if not frappe.db.table_exists(target_doctype):
        return "missing_table"
    return "ok"


class Validator:
    """Validates discovered link fields against the live database."""

    def __init__(
        self,
        batch_size: int = queries.DEFAULT_BATCH_SIZE,
        limit: int | None = None,
        severity_map: dict | None = None,
    ):
        self.batch_size = batch_size
        self.limit = limit
        self.severity_map = severity_map or {}

        self.findings: list[Finding] = []
        self.config_issues: list[ConfigIssue] = []
        self.records_checked = 0
        self.broken_total = 0

    # -- static Link fields ------------------------------------------------

    def validate_link_field(self, lf: LinkField) -> None:
        target = (lf.target_doctype or "").strip()

        if not target:
            self.config_issues.append(
                ConfigIssue(lf.doctype, lf.fieldname, "Link field has no target DocType defined")
            )
            return

        status = get_target_status(target)
        if status in ("missing", "missing_table"):
            self.config_issues.append(
                ConfigIssue(
                    lf.doctype,
                    lf.fieldname,
                    f"Link field targets DocType '{target}' which does not exist in this site",
                )
            )
            return
        if status == "virtual":
            return

        self.records_checked += self._count(queries.populated_count_query(lf.doctype, lf.fieldname))
        self.broken_total += self._count(queries.broken_count_query(lf.doctype, lf.fieldname, target))

        severity = classify_severity(target, self.severity_map)
        last = ""
        while not self._capped():
            rows = frappe.db.sql(
                queries.broken_batch_query(lf.doctype, lf.fieldname, target),
                {"last": last, "batch": self.batch_size},
                as_dict=True,
            )
            if not rows:
                break
            for row in rows:
                self.findings.append(
                    Finding(
                        source_doctype=lf.doctype,
                        source_name=row.source_name,
                        fieldname=lf.fieldname,
                        target_doctype=target,
                        value=row.value,
                        reason=REASON_TARGET_NOT_FOUND,
                        severity=severity,
                    )
                )
            last = rows[-1].source_name

    # -- Dynamic Link fields ------------------------------------------------

    def validate_dynamic_field(self, lf: LinkField) -> None:
        doctype_field = (lf.doctype_field or "").strip()
        name_field = lf.fieldname

        if not doctype_field:
            self.config_issues.append(
                ConfigIssue(lf.doctype, lf.fieldname, "Dynamic Link field has no DocType field configured")
            )
            return

        self.records_checked += self._count(
            queries.dynamic_populated_count_query(lf.doctype, doctype_field, name_field)
        )

        last = ""
        while not self._capped():
            rows = frappe.db.sql(
                queries.dynamic_populated_batch_query(lf.doctype, doctype_field, name_field),
                {"last": last, "batch": self.batch_size},
                as_dict=True,
            )
            if not rows:
                break
            self._validate_dynamic_batch(lf, rows)
            last = rows[-1].source_name

    def _validate_dynamic_batch(self, lf: LinkField, rows) -> None:
        # group (value, source_name) pairs by the target DocType
        grouped: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            grouped.setdefault(row.target_doctype, []).append((row.value, row.source_name))

        for target, pairs in grouped.items():
            status = get_target_status(target)
            if status in ("missing", "missing_table", "virtual"):
                if status != "virtual":
                    self.config_issues.append(
                        ConfigIssue(
                            lf.doctype,
                            lf.fieldname,
                            f"Dynamic Link references invalid DocType '{target}'",
                        )
                    )
                continue

            existing = self._existing_names(target, [p[0] for p in pairs])
            severity = classify_severity(target, self.severity_map)

            for value, source_name in pairs:
                if value in existing:
                    continue
                self.broken_total += 1
                self.findings.append(
                    Finding(
                        source_doctype=lf.doctype,
                        source_name=source_name,
                        fieldname=lf.fieldname,
                        target_doctype=target,
                        value=value,
                        reason=REASON_TARGET_NOT_FOUND,
                        severity=severity,
                    )
                )

    # -- helpers ------------------------------------------------------------

    def _count(self, sql: str) -> int:
        return frappe.db.sql(sql)[0][0]

    def _existing_names(self, target_doctype: str, values: list[str]) -> set:
        """Return the subset of ``values`` that exist in ``tab{target}``."""
        existing: set[str] = set()
        step = queries.EXISTENCE_BATCH_SIZE
        for i in range(0, len(values), step):
            chunk = values[i : i + step]
            rows = frappe.db.sql(queries.names_in_batch_query(target_doctype, len(chunk)), tuple(chunk))
            existing.update(r[0] for r in rows)
        return existing

    def _capped(self) -> bool:
        return bool(self.limit) and len(self.findings) >= self.limit
