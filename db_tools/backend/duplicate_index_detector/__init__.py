"""Duplicate Index Detector.

Compares every index in the database and reports exact duplicates and
redundant prefix indexes (an index whose columns are a leading subset of a
wider index). Strictly read-only — it only *suggests* DROP statements.
"""
