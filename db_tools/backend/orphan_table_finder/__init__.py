"""Orphan Table Finder.

Reports ``tab*`` tables with no matching DocType record (leftovers from
uninstalled apps or renamed DocTypes) and, in reverse, DocTypes whose table is
missing from the database. Strictly read-only.
"""
