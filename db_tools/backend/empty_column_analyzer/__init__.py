"""Empty Column Analyzer.

Finds columns that are never populated (every row NULL or blank) and columns
that hold exactly one distinct value across the whole table — both are dead
weight carried on every read, and usually mark a field nobody ever filled in.

Aggregates are batched: one query per chunk of columns, never per row.
Strictly read-only.
"""
