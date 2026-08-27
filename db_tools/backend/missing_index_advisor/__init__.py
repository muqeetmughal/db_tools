"""Missing Index Advisor.

Suggests indexes for columns Frappe filters and joins on most — Link fields,
fields flagged ``search_index``, child-table ``parent`` columns and DocType
sort fields — when the underlying table is large enough to benefit.
"""
