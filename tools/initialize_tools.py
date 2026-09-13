"""
Tool Initialization

Inicijalizira i registrira sve tool-ove u tool registry
Treba se pozvati na početku aplikacije
"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)


def initialize_all_tools() -> Dict[str, bool]:
    """
    Inicijalizira i registrira sve tool-ove u tool registry

    Returns:
        Dictionary s statusom registracije po API-ju
    """
    from tools.tool_registry import get_tool_registry
    from tools.api_implementations import register_all_tools

    logger.info("=" * 60)
    logger.info("Initializing Tool Registry")
    logger.info("=" * 60)

    # Get tool registry
    tool_registry = get_tool_registry()

    # Register all API implementations
    logger.info("Registering API implementations...")
    status = register_all_tools(tool_registry)

    # Log results
    logger.info("\n" + "Registration Summary:")
    for api_name, is_registered in status.items():
        status_icon = "[OK]" if is_registered else "[FAIL]"
        logger.info(f"  {status_icon} {api_name.upper()}: {'Registered' if is_registered else 'Failed'}")

    # Count registered tools
    total_tools = len(tool_registry)
    logger.info(f"\nTotal registered tools: {total_tools}")

    # Log available tools by category
    if total_tools > 0:
        logger.info("\nAvailable tools by category:")
        for category in ['gmail', 'drive', 'calendar', 'docs', 'sheets', 'contacts', 'tasks']:
            tools = tool_registry.get_tools_by_category(category)
            if tools:
                logger.info(f"  {category.upper()}: {len(tools)} tools")
                for tool_name in tools:
                    logger.debug(f"    - {tool_name}")

    logger.info("=" * 60)

    return status


def is_tools_initialized() -> bool:
    """
    Provjeri jesu li tool-ovi inicijalizirani

    Returns:
        True ako postoje registrirani tool-ovi
    """
    from tools.tool_registry import get_tool_registry

    tool_registry = get_tool_registry()
    return len(tool_registry) > 0


def ensure_tools_initialized() -> None:
    """
    Osigurava da su tool-ovi inicijalizirani
    Poziva initialize_all_tools() ako tool-ovi još nisu registrirani
    """
    if not is_tools_initialized():
        logger.info("Tools not initialized, initializing now...")
        initialize_all_tools()
    else:
        logger.debug("Tools already initialized")


# Automatic initialization when module is imported
# Može se disable-ati postavljanjem environment varijable
import os
if os.getenv('AUTO_INITIALIZE_TOOLS', 'true').lower() == 'true':
    try:
        logger.info("Auto-initializing tools...")
        initialize_all_tools()
    except Exception as e:
        logger.error(f"Auto-initialization failed: {e}")
        logger.info("Tools will be initialized on first use")


if __name__ == "__main__":
    # CLI execution
    import sys

    print("Google Workspace ADK - Tool Initialization")
    print("=" * 60)

    try:
        status = initialize_all_tools()

        print("\nInitialization complete!")
        print(f"\nRegistered APIs:")
        for api_name, is_registered in status.items():
            status_icon = "[OK]" if is_registered else "[FAIL]"
            print(f"  {status_icon} {api_name}")

        sys.exit(0)

    except Exception as e:
        print(f"\n[FAIL] Initialization failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
