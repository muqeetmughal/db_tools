"""Data models for the Broken Link Detector.

Pure dataclasses — no Frappe or DB dependencies, so they stay testable and
renderer-agnostic.
"""

from dataclasses import asdict, dataclass, field

SEVERITIES = ("critical", "warning", "info")


@dataclass
class Finding:
    """A single broken link or invalid reference found in the data."""

    source_doctype: str
    source_name: str
    fieldname: str
    target_doctype: str
    value: str
    reason: str
    severity: str

    def to_dict(self):
        return asdict(self)


@dataclass
class ConfigIssue:
    """A metadata / configuration problem (reported separately from data)."""

    doctype: str
    fieldname: str
    message: str
    severity: str = "critical"

    def to_dict(self):
        return asdict(self)


@dataclass
class LinkField:
    """Discovered metadata for one link-like field."""

    doctype: str
    fieldname: str
    target_doctype: str = ""  # static target for Link fields
    fieldtype: str = "Link"
    doctype_field: str = ""  # Dynamic Link: field holding the target DocType
    is_custom: bool = False
    source_is_child: bool = False

    @property
    def is_dynamic(self) -> bool:
        return self.fieldtype == "Dynamic Link"


@dataclass
class Summary:
    """Counters summarising a scan run."""

    doctypes_scanned: int = 0
    link_fields_scanned: int = 0
    dynamic_link_fields_scanned: int = 0
    records_checked: int = 0
    broken_links: int = 0
    config_issues: int = 0
    execution_time: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass
class Report:
    """Structured output of a scan run."""

    summary: Summary
    findings: list = field(default_factory=list)
    config_issues: list = field(default_factory=list)

    def group_by(self) -> dict:
        """Group findings by source doctype, target doctype and severity."""
        by_doctype = {}
        by_target = {}
        by_severity = {s: 0 for s in SEVERITIES}

        for f in self.findings:
            by_doctype[f.source_doctype] = by_doctype.get(f.source_doctype, 0) + 1
            by_target[f.target_doctype] = by_target.get(f.target_doctype, 0) + 1
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1

        return {
            "by_doctype": by_doctype,
            "by_target_doctype": by_target,
            "by_severity": by_severity,
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
            "config_issues": [c.to_dict() for c in self.config_issues],
            "grouped": self.group_by(),
        }
