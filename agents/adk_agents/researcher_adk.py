"""
Researcher ADK Agent

Native ADK implementation of Deep Research agent using LlmAgent primitive.
Replaces custom BaseAgent with proper ADK architecture.

Capabilities:
- Deep web research using Google Search Grounding
- YouTube video transcript analysis
- Web scraping (optimized for Croatian portals)
- ReAct loop for iterative research
- Multi-source synthesis

Usage:
    from agents.adk_agents.researcher_adk import create_researcher_agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    # Create agent
    researcher = create_researcher_agent()

    # Create runner
    runner = Runner(
        agent=researcher,
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

from tools.adk_tools.research_adk_tools import get_research_adk_tools
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_researcher_agent(
    model: str = "gemini-3.5-flash",
    credentials=None
) -> LlmAgent:
    """
    Create Researcher ADK agent for deep web research.

    This agent specializes in:
    - Google Search with Grounding (Vertex AI)
    - YouTube transcript extraction and analysis
    - Web scraping (optimized for Croatian portals)
    - Multi-source research synthesis
    - ReAct (Reasoning-Action-Observation) pattern

    Research Modes:
    1. SIMPLE: Single search with quick answer (for: "What is X?")
    2. DEEP: Multi-step research with synthesis (for: "Research topic X")
    3. NEWS: Croatian news portal scraping (for: "Find news about X")
    4. COMPARATIVE: Multiple source comparison (for: "Compare X and Y")

    All tools are FREE - no external API keys needed!
    Uses Vertex AI credentials from environment.

    Args:
        model: Gemini model to use (default: "gemini-3.5-flash")
        credentials: Optional credentials. If None, uses env credentials.

    Returns:
        LlmAgent instance configured for research operations

    Example:
        >>> researcher = create_researcher_agent()
        >>> runner = Runner(agent=researcher, app_name="agents", session_service=InMemorySessionService())
        >>> # Use runner_utils for simplified execution
        >>> from agents.adk_agents.runner_utils import run_agent_simple
        >>> response = await run_agent_simple(researcher, "Research Google ADK in depth")
    """

    # Get Research tools
    research_tools = get_research_adk_tools(credentials=credentials)

    # Create agent using factory
    agent = create_adk_agent(
        name="researcher",
        model=model,
        description="Deep research specialist using Google Search, YouTube transcripts, and web scraping",
        tools=research_tools,
        load_instruction_from_file=True,  # Will load from agents/researcher/instructions.md
        config={
            "temperature": 0.6,  # Balanced for creativity and accuracy
            # A price report with a table and full source URLs runs past 8k, and
            # on Claude the thinking tokens come out of the same budget — 8192 was
            # also exactly _CLAUDE_THINKING_MIN_OUTPUT_TOKENS, leaving no headroom.
            "max_tokens": 16384,
            # Note: max_iterations handled by LlmAgent's ReAct implementation
        }
    )

    logger.info(f"Researcher ADK agent created with {len(research_tools)} research tools")
    return agent


# Create singleton instance for easy import
researcher_agent = None


def get_researcher_agent(model: str = "gemini-3.5-flash", credentials=None) -> LlmAgent:
    """
    Get or create singleton Researcher agent instance.

    Args:
        model: Gemini model
        credentials: Optional credentials

    Returns:
        LlmAgent instance
    """
    global researcher_agent

    if researcher_agent is None:
        researcher_agent = create_researcher_agent(model=model, credentials=credentials)

    return researcher_agent


if __name__ == "__main__":
    # Test agent creation
    import asyncio

    async def test():
        agent = create_researcher_agent()
        print(f"[OK] Researcher ADK agent created: {agent.name}")
        print(f"   Model: {agent.model}")
        print(f"   Description: {agent.description}")
        print(f"   Tools: {len(agent.tools)}")
        print(f"   Instruction preview: {agent.instruction[:200]}...")

        # Display available tools
        print(f"\n[TOOLS] Available Research Tools:")
        for tool in agent.tools:
            tool_name = getattr(tool, '__name__', str(tool))
            print(f"   - {tool_name}")

        # Display capabilities
        from tools.adk_tools.research_adk_tools import get_research_capabilities
        capabilities = get_research_capabilities()

        print(f"\n[CAPABILITIES] Research Capabilities:")
        for cap_name, cap_info in capabilities.items():
            print(f"\n   {cap_name.upper()}:")
            print(f"   - {cap_info['description']}")
            print(f"   - No API key needed: {cap_info['no_api_key_needed']}")

    asyncio.run(test())
