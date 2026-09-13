"""
Secretary ADK Agent

Native ADK implementation of Google Calendar specialist agent using LlmAgent primitive.
Replaces custom BaseAgent with proper ADK architecture.

Capabilities:
- Intelligent event scheduling
- Conflict detection and resolution
- Timezone-aware operations
- Availability management

Usage:
    from agents.adk_agents.secretary_adk import create_secretary_agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    # Create agent
    secretary = create_secretary_agent(user_timezone="Europe/Zagreb")

    # Create runner
    runner = Runner(
        agent=secretary,
        app_name="agents",
        session_service=InMemorySessionService()
    )

    # Execute
    response = await runner.run_async(
        new_message=types.Content(...),
        session_id="session-123",
        user_id="user-123"
    )
"""

from typing import Optional
import logging
import sys
import os

# Add project root to path for standalone testing
if __name__ == "__main__":
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
    from dotenv import load_dotenv
    load_dotenv()

from google.adk.agents import LlmAgent

from tools.adk_tools.calendar_adk_tools import get_calendar_adk_tools
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_secretary_agent(
    model: str = "gemini-3.5-flash",
    credentials=None,
    user_timezone: str = "Europe/Zagreb"
) -> LlmAgent:
    """
    Create Secretary ADK agent for Google Calendar operations.

    This agent specializes in:
    - Creating and managing calendar events
    - Checking availability and finding free time slots
    - Detecting scheduling conflicts
    - Timezone-aware date/time handling
    - Natural language event scheduling

    The agent is optimized for fast, consistent scheduling decisions,
    using Gemini Flash model for quick responses.

    Args:
        model: Gemini model to use (default: "gemini-3.5-flash")
        credentials: Optional OAuth2 credentials. If None, uses token file.
        user_timezone: User's timezone (default: "Europe/Zagreb")

    Returns:
        LlmAgent instance configured for Calendar operations

    Example:
        >>> secretary = create_secretary_agent(user_timezone="America/New_York")
        >>> runner = Runner(agent=secretary, app_name="agents", session_service=InMemorySessionService())
        >>> # Use runner_utils for simplified execution
        >>> from agents.adk_agents.runner_utils import run_agent_simple
        >>> response = await run_agent_simple(secretary, "Schedule meeting tomorrow at 2pm")
    """

    # Get Calendar tools (incl. freebusy / slot proposal / meeting creation)
    calendar_tools = get_calendar_adk_tools(credentials=credentials)

    # Meeting lifecycle: contact resolution (with mandatory disambiguation)
    # and follow-up drafts. Draft only — the Calendar invite itself is sent
    # by calendar_create_meeting(send_updates="all").
    from tools.adk_tools.contacts_adk_tools import (
        contacts_get_by_name,
        contacts_search_people,
    )
    from tools.adk_tools.gmail_adk_tools import gmail_create_draft

    tools = calendar_tools + [
        contacts_get_by_name,
        contacts_search_people,
        gmail_create_draft,
    ]

    # Load base instruction from file
    instruction_file = os.path.join(
        os.path.dirname(__file__),
        "..",
        "secretary",
        "instructions.md"
    )

    try:
        with open(instruction_file, 'r', encoding='utf-8') as f:
            instruction = f.read()
    except Exception as e:
        logger.warning(f"Failed to load instruction file: {e}")
        instruction = "You are Secretary, a Google Calendar specialist."

    # Inject current datetime and timezone context (uses centralized helper)
    # Datum/vrijeme se NE ubacuje ovdje: to bi zamrznulo sat na trenutak
    # kad je agent stvoren. Predaje se predložak, a tvornica ga omota u
    # ADK instruction provider koji ga renderira pri svakom pozivu.

    # Create agent using factory
    agent = create_adk_agent(
        name="secretary",
        model=model,
        description=(
            "Google Calendar specialist: event scheduling, availability checks, "
            "meeting lifecycle (contact resolution, FreeBusy slot proposals, "
            "Meet invites, follow-up drafts)"
        ),
        tools=tools,
        instruction=instruction,  # Use custom instruction with datetime context
        load_instruction_from_file=False,  # We already loaded and modified it
        config={
            "temperature": 0.3,  # Consistent scheduling decisions
            "max_tokens": 2048,
        }
    )

    # Store timezone for reference
    agent._user_timezone = user_timezone

    logger.info(f"Secretary ADK agent created with {len(tools)} tools")
    logger.info(f"User timezone: {user_timezone}")
    return agent


# Create singleton instance for easy import
secretary_agent = None


def get_secretary_agent(
    model: str = "gemini-3.5-flash",
    credentials=None,
    user_timezone: str = "Europe/Zagreb"
) -> LlmAgent:
    """
    Get or create singleton Secretary agent instance.

    Args:
        model: Gemini model
        credentials: Optional OAuth2 credentials
        user_timezone: User's timezone

    Returns:
        LlmAgent instance
    """
    global secretary_agent

    if secretary_agent is None:
        secretary_agent = create_secretary_agent(
            model=model,
            credentials=credentials,
            user_timezone=user_timezone
        )

    return secretary_agent


if __name__ == "__main__":
    # Test agent creation
    import asyncio

    async def test():
        agent = create_secretary_agent(user_timezone="Europe/Zagreb")
        print(f"[OK] Secretary ADK agent created: {agent.name}")
        print(f"   Model: {agent.model}")
        print(f"   Description: {agent.description}")
        print(f"   Tools: {len(agent.tools)}")
        print(f"   Timezone: {agent._user_timezone}")
        print(f"   Instruction preview: {agent.instruction[:200]}...")

        # Display available tools
        print(f"\n[TOOLS] Available Calendar Tools:")
        for tool in agent.tools:
            tool_name = getattr(tool, '__name__', str(tool))
            print(f"   - {tool_name}")

        print(f"\n[CAPABILITIES] Calendar Operations:")
        print("   - Schedule events intelligently")
        print("   - Check availability and find free time")
        print("   - Detect and resolve conflicts")
        print("   - Timezone-aware datetime handling")
        print("   - Update and delete events")

    asyncio.run(test())
