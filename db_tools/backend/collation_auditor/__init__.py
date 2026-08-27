"""Charset & Collation Auditor.

Mixed collations are a quiet source of both bugs and slow queries: joining two
varchar columns with different collations raises "Illegal mix of collations",
or silently prevents an index from being used.

Reports tables and columns that deviate from the database default.
Strictly read-only.
"""
