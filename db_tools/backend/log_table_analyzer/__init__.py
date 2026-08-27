"""Log Table Analyzer.

Frappe writes a lot of history: versions, error logs, activity, email queue,
scheduled job runs, route history. Left alone these grow without bound and
usually dominate a mature site's database.

Reports each log table's size, age span and growth rate, cross-referenced with
the retention configured in Log Settings. Strictly read-only.
"""
