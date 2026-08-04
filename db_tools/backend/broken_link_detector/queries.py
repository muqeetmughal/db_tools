"""SQL generation for the Broken Link Detector.

Every validator query is:
- JOIN-based (no ``frappe.db.exists`` / per-row lookups)
- batched via keyset pagination on ``parent.name``
- parameterised (values are never interpolated into SQL)
"""

from db_tools.backend.broken_link_detector.utils import quote_column, quote_table

# Max rows fetched per keyset-paginated batch.
DEFAULT_BATCH_SIZE = 1000

# Max values passed to a single ``IN (...)`` while validating dynamic links.
EXISTENCE_BATCH_SIZE = 500


def populated_count_query(doctype: str, fieldname: str) -> str:
    """Count rows where the link field holds a non-empty value."""
    col = quote_column(fieldname)
    return (
        f"SELECT COUNT(*) FROM {quote_table(doctype)} "
        f"WHERE {col} IS NOT NULL AND {col} != ''"
    )


def broken_count_query(doctype: str, fieldname: str, target_doctype: str) -> str:
    """Count broken links for a static Link field via a LEFT JOIN."""
    col = quote_column(fieldname)
    return (
        f"SELECT COUNT(*) FROM {quote_table(doctype)} parent "
        f"LEFT JOIN {quote_table(target_doctype)} target ON target.name = parent.{col} "
        f"WHERE parent.{col} IS NOT NULL AND parent.{col} != '' "
        f"AND target.name IS NULL"
    )


def broken_batch_query(doctype: str, fieldname: str, target_doctype: str) -> str:
    """Fetch the next batch of broken links (keyset pagination)."""
    col = quote_column(fieldname)
    return (
        f"SELECT parent.name AS source_name, parent.{col} AS value "
        f"FROM {quote_table(doctype)} parent "
        f"LEFT JOIN {quote_table(target_doctype)} target ON target.name = parent.{col} "
        f"WHERE parent.{col} IS NOT NULL AND parent.{col} != '' "
        f"AND target.name IS NULL "
        f"AND parent.name > %(last)s "
        f"ORDER BY parent.name LIMIT %(batch)s"
    )


def dynamic_populated_count_query(doctype: str, doctype_field: str, name_field: str) -> str:
    """Count rows where both halves of a Dynamic Link are populated."""
    dt_col = quote_column(doctype_field)
    name_col = quote_column(name_field)
    return (
        f"SELECT COUNT(*) FROM {quote_table(doctype)} "
        f"WHERE {name_col} IS NOT NULL AND {name_col} != '' "
        f"AND {dt_col} IS NOT NULL AND {dt_col} != ''"
    )


def dynamic_populated_batch_query(doctype: str, doctype_field: str, name_field: str) -> str:
    """Fetch the next batch of populated Dynamic Link rows."""
    dt_col = quote_column(doctype_field)
    name_col = quote_column(name_field)
    return (
        f"SELECT parent.name AS source_name, "
        f"parent.{name_col} AS value, parent.{dt_col} AS target_doctype "
        f"FROM {quote_table(doctype)} parent "
        f"WHERE parent.{name_col} IS NOT NULL AND parent.{name_col} != '' "
        f"AND parent.{dt_col} IS NOT NULL AND parent.{dt_col} != '' "
        f"AND parent.name > %(last)s "
        f"ORDER BY parent.name LIMIT %(batch)s"
    )


def names_in_batch_query(target_doctype: str, count: int) -> str:
    """Build ``SELECT name FROM tab{target} WHERE name IN (...?...)``.

    ``count`` placeholders are generated so values stay parameterised.
    """
    placeholders = ", ".join(["%s"] * count)
    return (
        f"SELECT name FROM {quote_table(target_doctype)} "
        f"WHERE name IN ({placeholders})"
    )
