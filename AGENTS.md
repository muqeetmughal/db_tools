# AGENTS.md — db_tools

Guidance for agents working in this app. Read this before adding or changing a tool.

## What this app is

A Frappe app that ships a suite of **read-only** database inspection tools, served as
website pages under `/db_tools`. There are no DocTypes and no desk pages — every tool is
a whitelisted API plus a Jinja page.

**All eighteen tools are implemented and working.** Nothing is stubbed.

## Layout

```
db_tools/
├── backend/
│   ├── __init__.py                 # TOOLS registry — drives the /db_tools hub page
│   ├── common.py                   # shared helpers: guard(), coercion, renderers
│   └── <tool_key>/
│       ├── __init__.py             # module docstring explaining the tool
│       └── api.py                  # pure logic + @frappe.whitelist() endpoint
├── templates/
│   └── db_tools_base.html          # shared CSS + the DBTool frontend engine
└── www/db_tools/
    ├── index.py / index.html       # the hub page
    └── <tool_key>/
        ├── index.py                # get_context — blocks Guest
        └── index.html              # extends the base template, supplies a config
```

`broken_link_detector` is the one tool split across several modules
(`scanner.py`, `validator.py`, `queries.py`, `models.py`, `report.py`) because it is
substantially more complex. Every other tool fits in a single `api.py`.

`database_tools/api.py` is a backwards-compatible shim re-exporting
`backend.schema_comparison.api`. Leave it alone unless removing it deliberately.

## The eighteen tools

Every endpoint is prefixed `db_tools.backend.`. `category` on each registry entry
drives the hub page's filter chips.

| # | Key | Category | Endpoint (`<key>.api.…`) |
|---|---|---|---|
| 01 | `schema_comparison` | Schema | `get_extra_db_columns_report` |
| 02 | `broken_link_detector` | Integrity | `get_broken_links_report` |
| 03 | `orphan_child_detector` | Integrity | `get_orphan_children_report` |
| 04 | `orphan_table_finder` | Schema | `get_orphan_tables_report` |
| 05 | `largest_tables_analyzer` | Storage | `get_largest_tables_report` |
| 06 | `duplicate_index_detector` | Performance | `get_duplicate_indexes_report` |
| 07 | `missing_index_advisor` | Performance | `get_missing_indexes_report` |
| 08 | `database_health_report` | Operations | `get_health_report` |
| 09 | `migration_sql_generator` | Operations | `get_migration_sql` |
| 10 | `site_schema_comparison` | Operations | `get_site_schema_comparison`, `get_sites` |
| 11 | `schema_drift_detector` | Schema | `get_schema_drift_report` |
| 12 | `empty_column_analyzer` | Storage | `get_empty_columns_report` |
| 13 | `log_table_analyzer` | Storage | `get_log_tables_report` |
| 14 | `customization_auditor` | Integrity | `get_customization_report` |
| 15 | `collation_auditor` | Schema | `get_collation_report` |
| 16 | `fragmentation_analyzer` | Performance | `get_fragmentation_report` |
| 17 | `row_size_auditor` | Schema | `get_row_size_report` |
| 18 | `mariadb_limits_auditor` | Schema | `get_limits_report` |

Tools 01/11, 05/12/13 and 17/18 are deliberately adjacent — extra columns vs missing
columns, size vs waste vs growth, the SQL-layer row limit vs every server limit.
Check whether an existing tool already covers a question before adding a nineteenth.

## Non-negotiable rules

1. **Read-only.** No tool may write, drop, delete or alter anything. The migration
   generator produces *text* for a human to review; it never executes it. If you add a
   destructive capability, that is a product decision — ask first.
2. **System Manager only — the whole app.** See *Access control* below. Never add
   `allow_guest=True` to anything here; these endpoints expose schema and row data.
3. **Batched SQL.** Use JOINs and `GROUP BY` against `information_schema` or the tables
   themselves. Never loop per row issuing a query — these tools run against sites with
   millions of rows.
4. **Quote identifiers** with `common.quote()`. DocType names contain spaces.
5. **MariaDB.** Call `require_mariadb()` in anything touching `information_schema`.
   Postgres is not supported; fail loudly rather than returning wrong numbers.

## Access control

The entire app is restricted to the **System Manager** role (and Administrator). Two
layers, both mandatory:

- **Pages** — every `www/db_tools/**/index.py`, including the hub at `/db_tools`, calls
  `guard_page(context)` from `common.py`. Guests get a 301 to
  `/login?redirect-to=<path>`; a logged-in user without the role gets a 403.
- **Endpoints** — every `@frappe.whitelist()` function calls `guard()` as its first
  statement. Without this a user could bypass the page and hit the API directly.

`guard()` allows Administrator, rejects Guest with "please log in", and otherwise
requires `System Manager` in `frappe.get_roles()`. To widen access, change
`ALLOWED_ROLES` in `common.py` — the one place it is defined.

Audit that nothing slipped through:

```bash
cd db_tools/backend && for f in */api.py; do python3 - "$f" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef) and any("whitelist" in ast.unparse(d) for d in n.decorator_list):
        print(("OK      " if "guard()" in ast.unparse(n) else "MISSING "), sys.argv[1], n.name)
PY
done
```

## Adding a new tool

1. Create `backend/<key>/__init__.py` (docstring) and `api.py`.
2. Write the logic as a plain function returning
   `{"<rows_key>": [...], "grouped": {...}, "summary": {...}}`, then a thin
   `@frappe.whitelist()` wrapper that calls `guard()`, coerces args with
   `as_bool` / `as_int` / `as_list`, and returns
   `respond(payload, fmt, "<rows_key>", COLUMNS, "Title")`.
   `respond` gives you CSV and Markdown downloads for free; `fmt="json"` returns the
   structured payload unchanged (the frontend stringifies it).
3. Create `www/db_tools/<key>/index.py` (copy an existing one — it blocks Guest).
4. Create `www/db_tools/<key>/index.html` extending `templates/db_tools_base.html`.
   Fill the Jinja blocks (`tool_name`, `tool_icon`, `options`, …) and call
   `DBTool.init({...})` inside `{% block script %}`.
5. Register it in `backend/__init__.py` `TOOLS` with `"status": "ready"`.

`guard()` in the endpoint and `guard_page(context)` in the page are not optional — a tool
without both is a data leak.

No `bench build` is needed — the CSS and JS live inside the Jinja template, not in
`public/`. A page edit is visible on reload.

## The DBTool frontend engine

Defined in `templates/db_tools_base.html`. A page supplies one config object; the engine
handles fetching, error toasts, stat cards, chips, sortable/searchable/filterable tables,
code panels with copy, and JSON/CSV/Markdown downloads.

```js
DBTool.init({
    api: "db_tools.backend.<key>.api.<method>",
    filename: "download_basename",
    statCount: 6,              // skeleton placeholders while loading
    autoRun: true,             // false if you must bootstrap first (see site_schema_comparison)
    params: () => ({ ... }),   // read the inputs in {% block options %}
    stats: (d) => [{ icon, label, value, color, bg, raw, paint }],
    chips: (d) => [{ label, value, color, icon }],
    sections: [{
        id, title, subtitle, icon, placeholder,
        rows: (d) => d.findings,
        filter: { label, key, options: [{value, label}] },
        columns: [{ key, label, type: "number", cls: "mono", render: (row) => html }],
        empty: { icon, title, text },
        hideWhenEmpty: true,
    }],
    afterRender: (d) => {},
});
```

Set `type: "code"` with `text: (d) => "..."` for a section that renders a `<pre>` with a
copy button instead of a table (see `migration_sql_generator`).

Always escape interpolated values in a `render` with `DBTool.esc(...)` — row values come
from the database.

## Deriving expected values — use Frappe, don't reimplement

Several tools compare the database against what Frappe *would* create. Always call
Frappe's own code for that; hand-rolled equivalents produce false positives:

- **Column definitions**: `frappe.database.schema.get_definition(fieldtype, precision,
  length, options=...)`, not a raw `frappe.db.type_map` lookup. The type map ignores
  per-field `length` and `precision` overrides — reading it directly produced 88 phantom
  "length mismatches" from fields like `Address.address_line1` (varchar(240)) and
  `Appraisal KRA.goal_score` (decimal(21,2)).
- **Fields with no column**: `frappe.model.no_value_fields`, which already includes
  `Table` and `Table MultiSelect`.
- **Declared storage engine**: `DocType.engine`. A few Frappe log DocTypes ask for
  MyISAM on purpose, so "not InnoDB" is only drift when it disagrees with that field.

## Known-good behaviour (verified against atlas.localhost)

- The pre-existing `db_tools/tests/test_broken_links.py` has **8 failing tests**
  (6 DB-integration tests that hit `LinkValidationError` building their fixtures,
  `test_render_json`, and `test_render_console`). These predate the current work and are
  unrelated to the other tools — do not assume you broke them.

These are correct results on a healthy site, not bugs:

- **Orphan Child Detector reports 0** on a clean site. Two false-positive classes are
  deliberately excluded: parenttypes starting with `__` (Frappe's `__default` /
  `__global` buckets on `tabDefaultValue`), and Single DocTypes, whose child rows
  legitimately carry `parent == parenttype` and have no `tab<DocType>` table.
- **Duplicate Index Detector reports ~22 redundant prefixes** on stock Frappe/ERPNext
  (e.g. `tabAccount.lft` covered by `lft_rgt_index (lft, rgt)`). These are real.
- **`PRIMARY` is never suggested for dropping**, and a unique index is never reported as
  redundant against a non-unique one.
- **`utf8mb4_bin` on a longtext column is not a collation problem** — that is MariaDB's
  own representation of a JSON column. The Collation Auditor skips it, and only treats a
  differing *charset* as critical; a different collation within utf8mb4 is info.
- **Row size has two different ceilings, and they need different maths.** The SQL-layer
  limit (65,535) counts every column's full declared width — that is what
  `row_size_auditor` measures. InnoDB additionally needs two rows per page (~8,127 bytes
  for a 16K page), but on DYNAMIC/COMPRESSED it may push any variable-length column
  off-page for a 20-byte pointer, so only `innodb_row_bytes()` in `mariadb_limits_auditor`
  models that correctly. Charging full varchar width against the page limit reported 130
  violations on tables that store perfectly well.
- **FULLTEXT and SPATIAL indexes are exempt from the 3072-byte key limit** — checking
  them flags `__global_search.content` at 2133% of a limit that does not apply to it.
- **Schema Drift reports ~20 missing columns and ~10 JSON→longtext mismatches** on a
  stock bench. Both are real: columns awaiting a migration, and JSON fields created
  before Frappe mapped them to a native `json` column.

## Testing

There is no test runner wired up. Verify changes directly:

```bash
cd sites && ../env/bin/python -c "
import frappe; frappe.init(site='<site>'); frappe.connect(); frappe.set_user('Administrator')
from db_tools.backend.<key>.api import <fn>
print(<fn>()['summary'])"
```

For an end-to-end check, hit the page and the endpoint over HTTP as an authenticated
System Manager and confirm a 200 plus a sane `summary`. When testing with an API token,
set `api_secret` via `frappe.utils.password.set_encrypted_password` (assigning
`user.api_secret` then `save()` does not persist it) — and remove the token afterwards.

Verify a detector actually *detects*, not just that it returns cleanly: insert a
synthetic bad row, scan, then `frappe.db.rollback()`.
