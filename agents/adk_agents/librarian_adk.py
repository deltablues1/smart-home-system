"""
Librarian ADK Agent

Native ADK implementation of Google Drive specialist agent using LlmAgent primitive.
Replaces custom BaseAgent with proper ADK architecture.

Capabilities:
- Natural language file search with automatic query translation
- File organization and folder management
- Permission and sharing management
- Drive Query Language support

Key Feature:
- NATURAL LANGUAGE SEARCH: Automatically translates conversational queries
  to Drive Query Language for precise file searching

Usage:
    from agents.adk_agents.librarian_adk import create_librarian_agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    # Create agent
    librarian = create_librarian_agent()

    # Create runner
    runner = Runner(
        agent=librarian,
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

from tools.adk_tools.drive_adk_tools import get_drive_adk_tools
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_librarian_agent(
    model: str = "gemini-3.5-flash",
    credentials=None,
    user_timezone: str = "Europe/Zagreb"
) -> LlmAgent:
    """
    Create Librarian ADK agent for Google Drive operations.

    This agent specializes in:
    - Natural language file search (converts queries to Drive QPL)
    - File organization and folder management
    - Permission and sharing management
    - File upload, update, and deletion

    The agent uses a NATURAL LANGUAGE SEARCH approach: it automatically
    translates conversational search queries ("find budget from last week")
    into proper Drive Query Language for precise file searching.

    The agent is optimized for efficient file operations using Flash model
    with low temperature for consistent, accurate results.

    Args:
        model: Gemini model to use (default: "gemini-3.5-flash")
        credentials: Optional OAuth2 credentials. If None, uses token file.

    Returns:
        LlmAgent instance configured for Drive operations

    Example:
        >>> librarian = create_librarian_agent()
        >>> runner = Runner(agent=librarian, app_name="agents", session_service=InMemorySessionService())
        >>> # Use runner_utils for simplified execution
        >>> from agents.adk_agents.runner_utils import run_agent_simple
        >>> response = await run_agent_simple(librarian, "Find budget spreadsheets from last month")
    """

    # Load instruction from markdown file
    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "librarian", "instructions.md"
    )

    with open(instruction_path, "r", encoding="utf-8") as f:
        instruction = f.read()

    # Inject current datetime and timezone context (uses centralized helper)
    # Datum/vrijeme se NE ubacuje ovdje: to bi zamrznulo sat na trenutak
    # kad je agent stvoren. Predaje se predložak, a tvornica ga omota u
    # ADK instruction provider koji ga renderira pri svakom pozivu.

    # Get Drive tools
    drive_tools = get_drive_adk_tools(credentials=credentials)

    # Create agent using factory
    agent = create_adk_agent(
        name="librarian",
        model=model,
        instruction=instruction,
        description="Google Drive specialist for file search, organization, and permission management with natural language query translation",
        tools=drive_tools,
        load_instruction_from_file=False,  # Already loaded and injected above
        config={
            "temperature": 0.3,  # Lower temperature for precise file operations
            "max_tokens": 1536,
        }
    )

    logger.info(f"Librarian ADK agent created with {len(drive_tools)} Drive tools")
    return agent


# Create singleton instance for easy import
librarian_agent = None


def get_librarian_agent(
    model: str = "gemini-3.5-flash",
    credentials=None
) -> LlmAgent:
    """
    Get or create singleton Librarian agent instance.

    Args:
        model: Gemini model
        credentials: Optional OAuth2 credentials

    Returns:
        LlmAgent instance
    """
    global librarian_agent

    if librarian_agent is None:
        librarian_agent = create_librarian_agent(
            model=model,
            credentials=credentials
        )

    return librarian_agent


if __name__ == "__main__":
    # Test agent creation
    import asyncio

    async def test():
        agent = create_librarian_agent()
        print(f"[OK] Librarian ADK agent created: {agent.name}")
        print(f"   Model: {agent.model}")
        print(f"   Description: {agent.description}")
        print(f"   Tools: {len(agent.tools)}")
        print(f"   Instruction preview: {agent.instruction[:200]}...")

        # Display available tools
        print(f"\n[TOOLS] Available Drive Tools:")
        for tool in agent.tools:
            tool_name = getattr(tool, '__name__', str(tool))
            print(f"   - {tool_name}")

        print(f"\n[KEY FEATURE] Natural Language Search:")
        print("   The agent ALWAYS uses translate_drive_query for natural language searches")
        print("   before using drive_search_files to find files.")
        print("\n   Workflow:")
        print("   1. User: 'find budget from last week'")
        print("   2. Agent translates to Drive QPL")
        print("   3. Agent searches with translated query")
        print("   4. Agent presents results in organized format")

        print(f"\n[CAPABILITIES] Drive Operations:")
        print("   - Natural language file search")
        print("   - File metadata and content retrieval")
        print("   - Upload and update files")
        print("   - Delete files (trash, recoverable)")
        print("   - Share files with permissions")
        print("   - Create and organize folders")
        print("   - Move files between folders")

    asyncio.run(test())
