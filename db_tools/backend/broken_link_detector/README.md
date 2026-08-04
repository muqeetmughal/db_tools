# Broken Link Detector

Validates referential integrity across the site: **Link** and **Dynamic Link**
fields referencing documents that no longer exist.

Strictly **read-only** — no updates, deletions, repairs or SQL execution.

## Architecture

```
backend/broken_link_detector/
├── broken_links.py   # Public API: detect_broken_links()
├── scanner.py        # Metadata discovery (Link / Dynamic Link / custom / child tables)
├── validator.py      # Validation engine (batched JOIN queries)
├── queries.py        # SQL generation
├── models.py         # Data models (Finding, ConfigIssue, Summary, Report)
├── report.py         # Output renderers (json / csv / markdown / console)
├── utils.py          # Severity classification + helpers
└── api.py            # Whitelisted endpoint for the web UI
```

Each layer is independent: the scanner knows nothing about rendering, the
validators never import renderers, and models are plain dataclasses.

## Public API

```python
from db_tools.backend.broken_link_detector.broken_links import detect_broken_links

report = detect_broken_links(
    doctype=None,          # str | list[str] | None
    include_dynamic=True,  # validate Dynamic Link fields
    include_child_tables=True,
    severity=None,         # "critical" | "warning" | "info" | None
    limit=None,            # cap collected findings
    batch_size=1000,       # rows per keyset-paginated batch
    severity_map=None,     # {DocType: "critical"|"warning"|"info"} overrides
)

report.summary.broken_links        # total broken links
report.summary.config_issues       # metadata problems (separate from data)
report.findings                    # list[Finding]
report.to_dict()                   # dict ready for JSON
report.group_by()                  # counts by doctype / target / severity
```

### Findings

```python
Finding(
    source_doctype="Sales Invoice",
    source_name="SINV-00018",
    fieldname="customer",
    target_doctype="Customer",
    value="CUST-00045",
    reason="Target document not found",
    severity="warning",   # critical | warning | info
)
```

### Renderers

```python
from db_tools.backend.broken_link_detector.report import render

render(report, "json")      # structured JSON
render(report, "csv")       # findings CSV (+ config issues section)
render(report, "markdown")  # markdown report
render(report, "console")   # aligned console table
```

New formats: add a `render_<name>(report)` function and register it in
`RENDERERS` (report.py).

## How validation works

- **Metadata** is loaded via `frappe.get_meta()` (includes custom fields and
  fields inside child tables). No hardcoded definitions.
- **Link fields** are validated with a single JOIN per field:

  ```sql
  SELECT parent.name, parent.`field`
  FROM `tab<dt>` parent
  LEFT JOIN `tab<target>` target ON target.name = parent.`field`
  WHERE parent.`field` IS NOT NULL AND parent.`field` != ''
    AND target.name IS NULL
    AND parent.name > %(last)s   -- keyset pagination
  ORDER BY parent.name LIMIT %(batch)s
  ```

- **Dynamic Link fields** stream populated rows in batches, group values by
  their target DocType, then check existence with batched
  `WHERE name IN (...)` queries (500 values per query). One round-trip per
  target per batch — never one query per document.
- **Empty values** (NULL / `""` / whitespace) are ignored.
- **Config issues** (target DocType missing, invalid Dynamic Link target) are
  reported separately from broken data.

## Severity classification

Default `critical`: User, Company, Account, Warehouse, Cost Center, Fiscal Year,
Department, Branch, DefaultValue, Series.

Default `warning`: Customer, Supplier, Employee, Item, Project, Sales Order,
Purchase Order, Address, Contact.

Everything else → `info`. Override per DocType via `severity_map`.

## Web UI

Dashboard at `/db_tools/broken_link_detector` (registered in `backend/__init__.py`):

- Scan options: DocType filter, severity, dynamic links, child tables, limit
- Summary cards + severity / doctype breakdown chips
- Searchable, sortable, filterable findings table
- Export to JSON / CSV / Markdown (reuses the backend renderers)

The UI calls `db_tools.backend.broken_link_detector.api.get_broken_links_report`
(`fmt=json|csv|markdown`).

## Limitations

- Link values are compared against the target table's `name` column.
- Virtual DocTypes are skipped (no physical table).
- Dynamic Link counts are exact only when no `limit` is applied.
- Findings are collected in memory; use `limit` for very large result sets.

## Tests

```bash
bench --site <site> run-tests --app db_tools --module test_broken_links
```

Covers severity classification, SQL generation, renderers, broken-link
detection, child tables, dynamic links, config issues, severity filter and
read-only guarantees.
