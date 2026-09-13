"""
Analyst ADK Agent

Native ADK implementation of Google Sheets specialist agent using LlmAgent primitive.
Replaces custom BaseAgent with proper ADK architecture.

Capabilities:
- Data analysis and insights
- Schema-first reading approach for efficient large spreadsheet handling
- Formula management
- Data manipulation and reporting

Key Feature:
- SCHEMA-FIRST APPROACH: Always read column headers before reading data
  to optimize context usage and understand data structure

Usage:
    from agents.adk_agents.analyst_adk import create_analyst_agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    # Create agent
    analyst = create_analyst_agent()

    # Create runner
    runner = Runner(
        agent=analyst,
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

from tools.adk_tools.sheets_adk_tools import get_sheets_adk_tools
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_analyst_agent(
    model: str = "gemini-3.5-flash",
    credentials=None
) -> LlmAgent:
    """
    Create Analyst ADK agent for Google Sheets operations.

    This agent specializes in:
    - Data analysis and insights from spreadsheets
    - Schema-first reading approach (read headers before data)
    - Creating and formatting spreadsheets
    - Writing formulas and calculations
    - Efficient handling of large datasets

    The agent uses a SCHEMA-FIRST approach: it always reads column headers
    first to understand the data structure, then asks what specific data
    to analyze. This saves tokens and optimizes context usage.

    The agent is optimized for precise data operations using Flash model
    with low temperature for consistent, accurate results.

    Args:
        model: Gemini model to use (default: "gemini-3.5-flash")
        credentials: Optional OAuth2 credentials. If None, uses token file.

    Returns:
        LlmAgent instance configured for Sheets operations

    Example:
        >>> analyst = create_analyst_agent()
        >>> runner = Runner(agent=analyst, app_name="agents", session_service=InMemorySessionService())
        >>> # Use runner_utils for simplified execution
        >>> from agents.adk_agents.runner_utils import run_agent_simple
        >>> response = await run_agent_simple(analyst, "Analyze sales data from spreadsheet 1abc...")
    """

    # Get Sheets tools
    sheets_tools = get_sheets_adk_tools(credentials=credentials)

    all_tools = list(sheets_tools)
    description = (
        "Google Sheets specialist for data analysis, schema-first reading, "
        "and efficient data manipulation."
    )

    # Create agent using factory
    agent = create_adk_agent(
        name="analyst",
        model=model,
        description=description,
        tools=all_tools,
        load_instruction_from_file=True,  # Will load from agents/analyst/instructions.md
        config={
            "temperature": 0.2,  # Low temperature for precise data operations
            "max_tokens": 2048,
        }
    )

    logger.info(f"Analyst ADK agent created with {len(all_tools)} tools")
    return agent


# Create singleton instance for easy import
analyst_agent = None


def get_analyst_agent(
    model: str = "gemini-3.5-flash",
    credentials=None
) -> LlmAgent:
    """
    Get or create singleton Analyst agent instance.

    Args:
        model: Gemini model
        credentials: Optional OAuth2 credentials

    Returns:
        LlmAgent instance
    """
    global analyst_agent

    if analyst_agent is None:
        analyst_agent = create_analyst_agent(
            model=model,
            credentials=credentials
        )

    return analyst_agent


if __name__ == "__main__":
    # Test agent creation
    import asyncio

    async def test():
        agent = create_analyst_agent()
        print(f"[OK] Analyst ADK agent created: {agent.name}")
        print(f"   Model: {agent.model}")
        print(f"   Description: {agent.description}")
        print(f"   Tools: {len(agent.tools)}")
        print(f"   Instruction preview: {agent.instruction[:200]}...")

        # Display available tools
        print(f"\n[TOOLS] Available Sheets Tools:")
        for tool in agent.tools:
            tool_name = getattr(tool, '__name__', str(tool))
            print(f"   - {tool_name}")

        print(f"\n[KEY FEATURE] Schema-First Approach:")
        print("   The agent ALWAYS uses read_sheets_schema BEFORE reading data")
        print("   from large spreadsheets to optimize context usage.")
        print("\n   Workflow:")
        print("   1. Read schema (column headers)")
        print("   2. Understand data structure")
        print("   3. Ask user which columns to analyze")
        print("   4. Read only relevant data")
        print("   5. Perform analysis and provide insights")

        print(f"\n[CAPABILITIES] Data Operations:")
        print("   - Read spreadsheet metadata and structure")
        print("   - Schema-first reading for large datasets")
        print("   - Data analysis and insights")
        print("   - Create and format spreadsheets")
        print("   - Write formulas and calculations")
        print("   - Batch updates for efficiency")

    asyncio.run(test())
