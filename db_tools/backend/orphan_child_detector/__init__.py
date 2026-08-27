"""Orphan Child Detector.

Finds rows in child tables (``istable = 1`` DocTypes) whose owning parent
document no longer exists, plus rows with a missing/invalid ``parenttype`` or
``parentfield``. Strictly read-only.
"""
