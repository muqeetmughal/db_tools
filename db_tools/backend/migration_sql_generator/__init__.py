"""Migration SQL Generator.

Turns the findings of the other tools into a reviewable SQL script — DROP for
orphan tables and duplicate indexes, ADD INDEX for missing indexes, DELETE for
orphan child rows, DROP COLUMN for schema drift.

The generator never executes anything: it only produces text for a human to
review and run.
"""
