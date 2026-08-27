"""Customization Auditor.

Checks the site's customisation layer for references that no longer resolve:
Custom Fields on DocTypes that were removed, Link fields pointing at a missing
target, custom fields with no database column, duplicates, and Property Setters
for fields that no longer exist.

These accumulate silently when an app is uninstalled or a field is renamed.
Strictly read-only.
"""
