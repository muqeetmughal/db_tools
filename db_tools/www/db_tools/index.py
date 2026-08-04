import frappe

from db_tools.backend import TOOLS


def get_context(context):
    context.no_cache = 1
    context.tools = TOOLS
    return context
