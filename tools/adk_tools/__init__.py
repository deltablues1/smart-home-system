"""
ADK Tools Package

Native Google ADK tool implementations using FunctionTool primitives.
These tools wrap existing API implementations in ADK-compliant format.

Lazy-loaded: submodules are imported on first access, not at package import.
This prevents importing the entire Google Workspace SDK stack when only
a single tool module (e.g. mqtt_adk_tools) is needed.
"""

import importlib

__all__ = [
    'gmail_adk_tools',
    'research_adk_tools',
    'docs_adk_tools',
    'calendar_adk_tools',
    'sheets_adk_tools',
    'drive_adk_tools',
    'contacts_adk_tools',
]


def __getattr__(name):
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
