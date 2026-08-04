"""Backwards-compatible shim.

The schema comparison tool now lives at
``db_tools.backend.schema_comparison.api``. This module re-exports the old
import paths for anything that still references them.
"""

from db_tools.backend.schema_comparison.api import (
    NO_DB_FIELDTYPES,
    STANDARD_COLUMNS,
    find_doctype_json_paths,
    find_extra_db_columns,
    get_expected_fields,
    get_extra_db_columns_report,
    print_extra_db_columns,
)

__all__ = [
    "NO_DB_FIELDTYPES",
    "STANDARD_COLUMNS",
    "find_doctype_json_paths",
    "find_extra_db_columns",
    "get_expected_fields",
    "get_extra_db_columns_report",
    "print_extra_db_columns",
]
