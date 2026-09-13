"""
Tools module for Google Workspace ADK

Provides MCP toolsets and custom function tools.
Lazy-loaded to avoid pulling in Google API clients at import time.
"""

import importlib

__all__ = [
    'format_markdown_for_docs',
    'get_docs_formatter_tool',
    'translate_drive_query',
    'get_drive_query_translator_tool',
    'read_sheets_schema',
    'get_sheets_schema_reader_tool',
]

def __getattr__(name):
    _custom_tools_map = {
        'format_markdown_for_docs': '.custom_tools.docs_formatter',
        'get_docs_formatter_tool': '.custom_tools.docs_formatter',
        'translate_drive_query': '.custom_tools.drive_query_translator',
        'get_drive_query_translator_tool': '.custom_tools.drive_query_translator',
        'read_sheets_schema': '.custom_tools.sheets_schema_reader',
        'get_sheets_schema_reader_tool': '.custom_tools.sheets_schema_reader',
    }
    if name in _custom_tools_map:
        module = importlib.import_module(_custom_tools_map[name], package=__name__)
        attr = getattr(module, name)
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
