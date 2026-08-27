"""MariaDB Limits Auditor.

Checks the schema against the hard limits MariaDB/InnoDB enforce — identifier
length, columns and indexes per table, index key bytes, inline row size and
AUTO_INCREMENT headroom — and reports how close each object is to its ceiling.

Limits are resolved from the live server (page size, default row format), not
assumed, so the numbers match what this server would actually reject.
Strictly read-only.
"""
