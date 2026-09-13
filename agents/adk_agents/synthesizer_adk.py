"""
Synthesizer ADK Agent

Native ADK implementation of professional editor and technical writer.
Uses Gemini Pro for high-quality synthesis and document creation.

Capabilities:
- Deep synthesis of research notes into cohesive narratives
- Structural formatting with logical organization
- Tone adaptation (Executive Summary, Technical Report, Blog Post, etc.)
- Citation preservation and formatting
- No hallucinations - works only with provided material

Usage:
    from agents.adk_agents.synthesizer_adk import create_synthesizer_agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    # Create agent
    synthesizer = create_synthesizer_agent()

    # Create runner
    runner = Runner(
        agent=synthesizer,
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

from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_synthesizer_agent(
    model: str = "gemini-3.5-flash",  # Tier 3: Fast quality writing
    credentials=None
) -> LlmAgent:
    """
    Create Synthesizer ADK agent for professional writing and synthesis.

    This agent specializes in:
    - Transforming raw research into cohesive narratives
    - Professional document formatting and structure
    - Tone adaptation (Executive Summary, Technical Report, Blog Post)
    - Citation preservation and proper formatting
    - Clear, hallucination-free synthesis

    Design Philosophy:
    - NO TOOLS NEEDED - Pure LLM capabilities with Pro model
    - Works with output from Researcher agent
    - Collaborates with Scribe agent for document creation
    - High-quality synthesis requires Pro model (not Flash)

    Args:
        model: Gemini model to use (default: "gemini-2.5-pro" for quality)
        credentials: Optional OAuth2 credentials (not used, included for consistency)

    Returns:
        LlmAgent instance configured for synthesis operations

    Example:
        >>> synthesizer = create_synthesizer_agent()
        >>> runner = Runner(agent=synthesizer, app_name="agents", session_service=InMemorySessionService())
        >>> # Use runner_utils for simplified execution
        >>> from agents.adk_agents.runner_utils import run_agent_simple
        >>> response = await run_agent_simple(
        ...     synthesizer,
        ...     "Synthesize this research into executive summary: [research_notes]"
        ... )
    """

    # Synthesizer needs NO TOOLS - pure LLM synthesis
    tools = []

    # Load instruction from file
    instruction_file = os.path.join(
        os.path.dirname(__file__),
        "..",
        "synthesizer",
        "instructions.md"
    )

    try:
        with open(instruction_file, 'r', encoding='utf-8') as f:
            instruction = f.read()
    except Exception as e:
        logger.warning(f"Failed to load instruction file: {e}")
        instruction = "You are Synthesizer, a professional editor and technical writer."

    # Create agent using factory
    agent = create_adk_agent(
        name="synthesizer",
        model=model,
        description="Professional editor and technical writer: transforms research notes into polished documents, adapts tone to format",
        tools=tools,  # NO TOOLS - pure synthesis
        instruction=instruction,
        load_instruction_from_file=False,  # We already loaded it
        config={
            "temperature": 0.7,  # Moderate creativity for professional writing
            "max_tokens": 8192,  # Long-form content generation
        }
    )

    logger.info(f"Synthesizer ADK agent created (pure LLM, no tools)")
    logger.info(f"Model: {model}")
    logger.info(f"Purpose: Professional synthesis and document writing")
    return agent


# Create singleton instance for easy import
synthesizer_agent = None


def get_synthesizer_agent(
    model: str = "gemini-3.5-flash",
    credentials=None
) -> LlmAgent:
    """
    Get or create singleton Synthesizer agent instance.

    Args:
        model: Gemini model
        credentials: Optional OAuth2 credentials

    Returns:
        LlmAgent instance
    """
    global synthesizer_agent

    if synthesizer_agent is None:
        synthesizer_agent = create_synthesizer_agent(
            model=model,
            credentials=credentials
        )

    return synthesizer_agent


if __name__ == "__main__":
    # Test agent creation
    import asyncio

    async def test():
        agent = create_synthesizer_agent()
        print(f"[OK] Synthesizer ADK agent created: {agent.name}")
        print(f"   Model: {agent.model}")
        print(f"   Description: {agent.description}")
        print(f"   Tools: {len(agent.tools)} (pure LLM - no tools needed)")
        print(f"   Instruction preview: {agent.instruction[:200]}...")

        print(f"\n[CAPABILITIES] Professional Writing Operations:")
        print("   - Deep synthesis of research notes")
        print("   - Structural formatting with logical organization")
        print("   - Tone adaptation (Executive Summary, Technical Report, Blog)")
        print("   - Citation preservation and formatting")
        print("   - Hallucination-free synthesis")

        print(f"\n[WORKFLOW] Typical Usage:")
        print("   1. Receive research notes from Researcher agent")
        print("   2. Synthesize into cohesive narrative")
        print("   3. Format with proper structure (Title, Summary, Sections)")
        print("   4. Optionally hand off to Scribe for document creation")

    asyncio.run(test())
