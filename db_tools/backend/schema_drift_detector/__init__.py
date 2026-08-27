"""Schema Drift Detector.

The other half of Schema Comparison: instead of database columns with no field,
this finds fields with no database column, and columns whose SQL type no longer
matches the fieldtype declared in the DocType.

Drift like this happens when a fieldtype is changed but the migration that would
have altered the column never ran. Strictly read-only.
"""
