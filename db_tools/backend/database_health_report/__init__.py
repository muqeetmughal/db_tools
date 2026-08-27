"""Database Health Report.

Runs the other db_tools checks and rolls them up into a single scored report.
The expensive scans (orphan child rows, broken links) are opt-in via the
``deep`` flag. Strictly read-only.
"""
