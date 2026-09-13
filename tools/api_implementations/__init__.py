"""
Google API Implementations

Real API implementations for Google Workspace services
"""

from typing import Dict, Any, List
import logging

logger = logging.getLogger(__name__)

# Import all API implementations
try:
    from .gmail_api import register_gmail_tools
    GMAIL_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Gmail API implementation not available: {e}")
    GMAIL_AVAILABLE = False

try:
    from .drive_api import register_drive_tools
    DRIVE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Drive API implementation not available: {e}")
    DRIVE_AVAILABLE = False

try:
    from .calendar_api import register_calendar_tools
    CALENDAR_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Calendar API implementation not available: {e}")
    CALENDAR_AVAILABLE = False

try:
    from .docs_api import register_docs_tools
    DOCS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Docs API implementation not available: {e}")
    DOCS_AVAILABLE = False

try:
    from .sheets_api import register_sheets_tools
    SHEETS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Sheets API implementation not available: {e}")
    SHEETS_AVAILABLE = False

try:
    from .contacts_api import register_contacts_tools
    CONTACTS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Contacts API implementation not available: {e}")
    CONTACTS_AVAILABLE = False

try:
    from .tasks_api import register_tasks_tools
    TASKS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Tasks API implementation not available: {e}")
    TASKS_AVAILABLE = False

try:
    from .google_search_api import register_google_search_tools
    GOOGLE_SEARCH_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Google Search API implementation not available: {e}")
    GOOGLE_SEARCH_AVAILABLE = False

try:
    from .youtube_api import register_youtube_tools
    YOUTUBE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"YouTube API implementation not available: {e}")
    YOUTUBE_AVAILABLE = False

try:
    from .web_scraper_api import register_web_scraper_tools
    WEB_SCRAPER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Web Scraper API implementation not available: {e}")
    WEB_SCRAPER_AVAILABLE = False

try:
    from .vision_api import register_vision_tools
    VISION_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Vision API implementation not available: {e}")
    VISION_AVAILABLE = False

try:
    from .vertex_ai import register_vertex_ai_tools
    VERTEX_AI_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Vertex AI implementation not available: {e}")
    VERTEX_AI_AVAILABLE = False


def register_all_tools(tool_registry) -> Dict[str, bool]:
    """
    Registrira sve dostupne API implementations u tool registry

    Args:
        tool_registry: ToolRegistry instance

    Returns:
        Dictionary s statusom registracije po API-ju
    """
    status = {}

    if GMAIL_AVAILABLE:
        try:
            register_gmail_tools(tool_registry)
            status['gmail'] = True
            logger.info("Gmail tools registered")
        except Exception as e:
            logger.error(f"Failed to register Gmail tools: {e}")
            status['gmail'] = False
    else:
        status['gmail'] = False

    if DRIVE_AVAILABLE:
        try:
            register_drive_tools(tool_registry)
            status['drive'] = True
            logger.info("Drive tools registered")
        except Exception as e:
            logger.error(f"Failed to register Drive tools: {e}")
            status['drive'] = False
    else:
        status['drive'] = False

    if CALENDAR_AVAILABLE:
        try:
            register_calendar_tools(tool_registry)
            status['calendar'] = True
            logger.info("Calendar tools registered")
        except Exception as e:
            logger.error(f"Failed to register Calendar tools: {e}")
            status['calendar'] = False
    else:
        status['calendar'] = False

    if DOCS_AVAILABLE:
        try:
            register_docs_tools(tool_registry)
            status['docs'] = True
            logger.info("Docs tools registered")
        except Exception as e:
            logger.error(f"Failed to register Docs tools: {e}")
            status['docs'] = False
    else:
        status['docs'] = False

    if SHEETS_AVAILABLE:
        try:
            register_sheets_tools(tool_registry)
            status['sheets'] = True
            logger.info("Sheets tools registered")
        except Exception as e:
            logger.error(f"Failed to register Sheets tools: {e}")
            status['sheets'] = False
    else:
        status['sheets'] = False

    if CONTACTS_AVAILABLE:
        try:
            register_contacts_tools(tool_registry)
            status['contacts'] = True
            logger.info("Contacts tools registered")
        except Exception as e:
            logger.error(f"Failed to register Contacts tools: {e}")
            status['contacts'] = False
    else:
        status['contacts'] = False

    if TASKS_AVAILABLE:
        try:
            register_tasks_tools(tool_registry)
            status['tasks'] = True
            logger.info("Tasks tools registered")
        except Exception as e:
            logger.error(f"Failed to register Tasks tools: {e}")
            status['tasks'] = False
    else:
        status['tasks'] = False

    if GOOGLE_SEARCH_AVAILABLE:
        try:
            register_google_search_tools(tool_registry)
            status['google_search'] = True
            logger.info("Google Search tools registered")
        except Exception as e:
            logger.error(f"Failed to register Google Search tools: {e}")
            status['google_search'] = False
    else:
        status['google_search'] = False

    if YOUTUBE_AVAILABLE:
        try:
            register_youtube_tools(tool_registry)
            status['youtube'] = True
            logger.info("YouTube tools registered")
        except Exception as e:
            logger.error(f"Failed to register YouTube tools: {e}")
            status['youtube'] = False
    else:
        status['youtube'] = False

    if WEB_SCRAPER_AVAILABLE:
        try:
            register_web_scraper_tools(tool_registry)
            status['web_scraper'] = True
            logger.info("Web Scraper tools registered")
        except Exception as e:
            logger.error(f"Failed to register Web Scraper tools: {e}")
            status['web_scraper'] = False
    else:
        status['web_scraper'] = False

    if VISION_AVAILABLE:
        try:
            register_vision_tools(tool_registry)
            status['vision'] = True
            logger.info("Vision tools registered")
        except Exception as e:
            logger.error(f"Failed to register Vision tools: {e}")
            status['vision'] = False
    else:
        status['vision'] = False

    if VERTEX_AI_AVAILABLE:
        try:
            register_vertex_ai_tools(tool_registry)
            status['vertex_ai'] = True
            logger.info("Vertex AI tools registered")
        except Exception as e:
            logger.error(f"Failed to register Vertex AI tools: {e}")
            status['vertex_ai'] = False
    else:
        status['vertex_ai'] = False

    registered_count = sum(1 for v in status.values() if v)
    logger.info(f"Registered {registered_count}/{len(status)} API tool sets")

    return status


__all__ = [
    'register_all_tools',
    'GMAIL_AVAILABLE',
    'DRIVE_AVAILABLE',
    'CALENDAR_AVAILABLE',
    'DOCS_AVAILABLE',
    'SHEETS_AVAILABLE',
    'CONTACTS_AVAILABLE',
    'TASKS_AVAILABLE',
    'GOOGLE_SEARCH_AVAILABLE',
    'YOUTUBE_AVAILABLE',
    'WEB_SCRAPER_AVAILABLE',
    'WEB_SCRAPER_AVAILABLE',
    'VISION_AVAILABLE',
    'VERTEX_AI_AVAILABLE',
    'GOOGLE_ADS_AVAILABLE',
]
