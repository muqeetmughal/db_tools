from db_tools.backend.common import guard_page


def get_context(context):
    context.no_cache = 1
    guard_page(context)
    return context
