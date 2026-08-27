"""Site Schema Comparison.

Compares the live database schema of two sites on this bench — which tables
and columns exist on one side but not the other, and where a shared column's
definition drifted.

Each site is opened with its own credentials from ``site_config.json`` using a
short-lived read-only connection; nothing is written and no credential is ever
returned to the browser.
"""
