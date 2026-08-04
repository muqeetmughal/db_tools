"""Output rendering for the Broken Link Detector.

Renderers are independent of the scan logic. Add a new format by writing a
``render_<name>(report)`` function and registering it in ``RENDERERS``.
"""

import csv
import io
import json

from db_tools.backend.broken_link_detector.models import Report


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str)


def render_csv(report: Report) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["# db_tools - Broken Link Detector"])
    writer.writerow(["summary", json.dumps(report.summary.to_dict())])
    writer.writerow([])
    writer.writerow(["source_doctype", "source_name", "fieldname", "target_doctype", "value", "reason", "severity"])

    for f in report.findings:
        writer.writerow(
            [f.source_doctype, f.source_name, f.fieldname, f.target_doctype, f.value, f.reason, f.severity]
        )

    if report.config_issues:
        writer.writerow([])
        writer.writerow(["# config issues"])
        writer.writerow(["doctype", "fieldname", "message", "severity"])
        for c in report.config_issues:
            writer.writerow([c.doctype, c.fieldname, c.message, c.severity])

    return buf.getvalue()


def render_markdown(report: Report) -> str:
    lines = [
        "# Broken Link Detector Report",
        "",
        "## Summary",
        "",
        f"- **DocTypes scanned**: {report.summary.doctypes_scanned}",
        f"- **Link fields scanned**: {report.summary.link_fields_scanned}",
        f"- **Dynamic Link fields scanned**: {report.summary.dynamic_link_fields_scanned}",
        f"- **Records checked**: {report.summary.records_checked:,}",
        f"- **Broken links**: {report.summary.broken_links}",
        f"- **Configuration issues**: {report.summary.config_issues}",
        f"- **Execution time**: {report.summary.execution_time:.2f}s",
        "",
    ]

    lines.append("## Findings")
    lines.append("")
    if report.findings:
        lines.append("| Severity | Source DocType | Source Name | Field | Target DocType | Value | Reason |")
        lines.append("|---|---|---|---|---|---|---|")
        for f in report.findings:
            lines.append(
                f"| {f.severity} | {f.source_doctype} | {f.source_name} | "
                f"{f.fieldname} | {f.target_doctype} | {f.value} | {f.reason} |"
            )
    else:
        lines.append("_No broken links found._")
    lines.append("")

    if report.config_issues:
        lines.append("## Configuration Issues")
        lines.append("")
        lines.append("| DocType | Field | Message | Severity |")
        lines.append("|---|---|---|---|")
        for c in report.config_issues:
            lines.append(f"| {c.doctype} | {c.fieldname} | {c.message} | {c.severity} |")
        lines.append("")

    return "\n".join(lines)


def render_console(report: Report) -> str:
    lines = []
    s = report.summary
    lines.append("=" * 76)
    lines.append("BROKEN LINK DETECTOR")
    lines.append("=" * 76)
    lines.append(f"DocTypes scanned             : {s.doctypes_scanned}")
    lines.append(f"Link fields scanned          : {s.link_fields_scanned}")
    lines.append(f"Dynamic Link fields scanned  : {s.dynamic_link_fields_scanned}")
    lines.append(f"Records checked              : {s.records_checked:,}")
    lines.append(f"Broken links                 : {s.broken_links}")
    lines.append(f"Configuration issues         : {s.config_issues}")
    lines.append(f"Execution time               : {s.execution_time:.2f}s")
    lines.append("-" * 76)

    if report.findings:
        header = ("SEV      SOURCE DOCTYPE        FIELD            TARGET DOCTYPE      VALUE")
        lines.append(header)
        lines.append("-" * 76)
        for f in report.findings[:200]:
            lines.append(
                f"{f.severity[:7].ljust(8)} {f.source_doctype[:21].ljust(22)} "
                f"{f.fieldname[:16].ljust(17)} {f.target_doctype[:19].ljust(20)} {f.value}"
            )
        if len(report.findings) > 200:
            lines.append(f"... and {len(report.findings) - 200} more")
    else:
        lines.append("No broken links found.")

    if report.config_issues:
        lines.append("-" * 76)
        lines.append("CONFIGURATION ISSUES")
        lines.append("-" * 76)
        for c in report.config_issues:
            lines.append(f"[{c.severity.upper()}] {c.doctype}.{c.fieldname}: {c.message}")

    lines.append("=" * 76)
    return "\n".join(lines)


RENDERERS = {
    "json": render_json,
    "csv": render_csv,
    "markdown": render_markdown,
    "md": render_markdown,
    "console": render_console,
    "text": render_console,
}


def render(report: Report, fmt: str) -> str:
    """Render a report in the requested format."""
    renderer = RENDERERS.get(fmt)
    if renderer is None:
        raise ValueError(f"Unknown report format: {fmt!r}. Supported: {', '.join(RENDERERS)}")
    return renderer(report)
