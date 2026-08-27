"""Table Fragmentation Analyzer.

Deletes and updates leave free space inside a table's pages that the engine will
not return to the filesystem on its own. Reports tables where that overhead is
worth an OPTIMIZE TABLE, and separately audits storage engine and row format.

Strictly read-only — OPTIMIZE statements are suggested, never executed.
"""
