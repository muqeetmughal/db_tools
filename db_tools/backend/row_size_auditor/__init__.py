"""Row Size Auditor.

InnoDB caps a row at roughly 65,535 bytes of inline data regardless of page
size. A DocType that accumulates enough Data/Select/Link custom fields will
eventually fail to alter with "Row size too large" — usually mid-migration.

Estimates each table's inline row size and column count so the tables closing in
on that ceiling show up before a migration breaks. Strictly read-only.
"""
