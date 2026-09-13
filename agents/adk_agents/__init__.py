"""
ADK Agents Package

Native Google ADK agent implementations using LlmAgent, SequentialAgent,
ParallelAgent, and LoopAgent primitives.
"""

from .mailer_adk import create_mailer_agent
from .researcher_adk import create_researcher_agent
from .scribe_adk import create_scribe_agent
from .secretary_adk import create_secretary_agent
from .analyst_adk import create_analyst_agent
from .librarian_adk import create_librarian_agent
from .rolodex_adk import create_rolodex_agent
from .tracker_adk import create_tracker_agent
from .scraper_adk import create_scraper_agent
from .voice_qa_adk import create_voice_qa_agent

__all__ = [
    'create_mailer_agent',
    'create_researcher_agent',
    'create_scribe_agent',
    'create_secretary_agent',
    'create_analyst_agent',
    'create_librarian_agent',
    'create_rolodex_agent',
    'create_tracker_agent',
    'create_scraper_agent',
    'create_voice_qa_agent',
]
