"""
Scribe ADK Agent

Native ADK implementation of Google Docs specialist agent using LlmAgent primitive.
Replaces custom BaseAgent with proper ADK architecture.

Capabilities:
- Professional document creation
- Markdown to Docs conversion
- Complex formatting operations
- Document structure management

Usage:
    from agents.adk_agents.scribe_adk import create_scribe_agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    # Create agent
    scribe = create_scribe_agent()

    # Create runner
    runner = Runner(
        agent=scribe,
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

from tools.adk_tools.docs_adk_tools import get_docs_adk_tools
from tools.adk_tools.drive_adk_tools import drive_share_file
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_scribe_agent(
    model: str = "gemini-3.5-flash",  # Tier 3: Fast document creation
    credentials=None
) -> LlmAgent:
    """
    Create Scribe ADK agent for Google Docs operations.

    This agent specializes in:
    - Creating professional documents
    - Converting Markdown to formatted Docs
    - Applying complex formatting (headings, bold, lists, etc.)
    - Managing document structure
    - Reading and updating existing documents

    The agent is optimized for quality writing and formatting, using
    Gemini Pro model for better comprehension and structure.

    Document Creation Workflow:
    1. Agent composes content in Markdown format
    2. Calls format_markdown_for_docs(markdown) to get batch requests
    3. Creates document with docs_create_document(title)
    4. Applies formatting with docs_batch_update(document_id, requests)
    5. Returns document URL and summary

    Args:
        model: Gemini model to use (default: "gemini-2.5-pro" for quality)
        credentials: Optional OAuth2 credentials. If None, uses token file.

    Returns:
        LlmAgent instance configured for Docs operations

    Example:
        >>> scribe = create_scribe_agent()
        >>> runner = Runner(agent=scribe, app_name="agents", session_service=InMemorySessionService())
        >>> # Use runner_utils for simplified execution
        >>> from agents.adk_agents.runner_utils import run_agent_simple
        >>> response = await run_agent_simple(scribe, "Create a meeting agenda for Q1 planning")
    """

    # Get Docs tools
    docs_tools = get_docs_adk_tools(credentials=credentials)

    # Add drive_share_file for sharing created documents
    # This is CRITICAL for workflows - documents must be shared after creation
    tools = docs_tools + [drive_share_file]

    # Create agent using factory
    agent = create_adk_agent(
        name="scribe",
        model=model,
        description="Google Docs specialist for creating and formatting professional documents",
        tools=tools,  # Includes Docs tools + drive_share_file
        load_instruction_from_file=True,  # Will load from agents/scribe/instructions.md
        config={
            "temperature": 0.7,  # Creative but consistent
            "max_tokens": 4096,  # Larger for document generation
        }
    )

    logger.info(f"Scribe ADK agent created with {len(tools)} tools (Docs + sharing)")
    return agent


# Create singleton instance for easy import
scribe_agent = None


def get_scribe_agent(model: str = "gemini-3.5-flash", credentials=None) -> LlmAgent:
    """
    Get or create singleton Scribe agent instance.

    Args:
        model: Gemini model
        credentials: Optional OAuth2 credentials

    Returns:
        LlmAgent instance
    """
    global scribe_agent

    if scribe_agent is None:
        scribe_agent = create_scribe_agent(model=model, credentials=credentials)

    return scribe_agent


if __name__ == "__main__":
    # Test agent creation
    import asyncio

    async def test():
        agent = create_scribe_agent()
        print(f"[OK] Scribe ADK agent created: {agent.name}")
        print(f"   Model: {agent.model}")
        print(f"   Description: {agent.description}")
        print(f"   Tools: {len(agent.tools)}")
        print(f"   Instruction preview: {agent.instruction[:200]}...")

        # Display available tools
        print(f"\n[TOOLS] Available Docs Tools:")
        for tool in agent.tools:
            tool_name = getattr(tool, '__name__', str(tool))
            print(f"   - {tool_name}")

        print(f"\n[CAPABILITIES] Document Operations:")
        print("   - Create professional documents")
        print("   - Markdown-to-Docs conversion")
        print("   - Complex formatting (headings, bold, lists)")
        print("   - Document structure management")
        print("   - Read and update existing documents")

    asyncio.run(test())
