"""
Socrates ADK Agent Wrapper

Wrapper around the existing philosophy/Socrates agent for orchestrator integration.
Maintains the original custom BaseAgent implementation while making it accessible
via standard orchestrator routing.

The Socrates agent uses:
- Gemini 3.0 Pro Preview with "Thinking Mode" for deep reasoning
- Philosophy RAG tool for knowledge base access
- Socratic method: Responds with questions, not answers
- Never leaves character as ancient Greek philosopher

Usage:
    from agents.adk_agents.socrates_adk import get_socrates_agent

    # Get the existing Socrates agent
    socrates = get_socrates_agent()

    # Can be used as sub-agent in orchestrator
    orchestrator = create_smart_orchestrator(
        worker_agents=[socrates, ...]
    )
"""

from typing import Optional
import logging
import sys
import os

# Add project root to path
if __name__ == "__main__":
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
    from dotenv import load_dotenv
    load_dotenv()

from google.adk.agents import LlmAgent

logger = logging.getLogger(__name__)


def create_socrates_agent(
    model: str = "gemini-3.5-flash",  # Tier 2: GA workhorse (matches registry)
    credentials=None
) -> LlmAgent:
    """
    Create or return the Socrates philosophy agent.

    This agent specializes in:
    - Socratic method: Teaching through questions, not answers
    - Deep philosophical reasoning with Gemini 3.0 "Thinking Mode"
    - Philosophy knowledge base (RAG) for historical context
    - Never breaking character as ancient Greek philosopher
    - Using counter-questions to challenge user assumptions
    - Analogies and reductio ad absurdum techniques

    IMPORTANT: This is a wrapper around the existing custom implementation
    in agents/philosophy/philosophy_agents.py. It maintains the original
    Socratic dialogue system.

    Args:
        model: Gemini model (default: "gemini-3-pro-preview" for Thinking Mode)
        credentials: Optional OAuth2 credentials (not used by philosophy agent)

    Returns:
        LlmAgent instance configured for Socratic dialogue

    Example:
        >>> socrates = create_socrates_agent()
        >>> # User: "Sokrat, što je istina?"
        >>> # Socrates: "Zanimljivo pitanje! Ali reci mi prvo - kako znaš da je nešto istinito?"
    """

    # Build Socrates directly (avoid importing philosophy_agents.py which
    # initialises Vertex AI at module level and blocks startup on Windows)
    from google.adk.agents import LlmAgent
    from tools.classroom_tools import get_philosophy_rag_tool

    rag_tool = get_philosophy_rag_tool()
    tools = [rag_tool] if rag_tool is not None else []

    # Single source of truth for the Socratic persona — shared with the CLI
    # philosophy classroom. Fallback below only if the file is unreadable.
    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "philosophy", "socrates_instructions.md"
    )
    try:
        with open(instruction_path, "r", encoding="utf-8") as f:
            instruction = f.read()
    except OSError:
        logger.warning("socrates_instructions.md not readable, using fallback instruction")
        instruction = """Ti si Sokrat, antički grčki filozof. Tvoj cilj nije dati odgovor, već voditi učenika do spoznaje.

Pravila:
1. Nikada ne odgovaraj direktno na pitanje.
2. Postavi jedno protu-pitanje po odgovoru koje izaziva pretpostavku korisnika.
3. Koristi analogije iz klasične filozofije i svakodnevnog života.
4. Ako korisnik tvrdi nešto nelogično, koristi 'reductio ad absurdum'.
5. Nikada ne izlazi iz lika. Ti si antički filozof.
6. Budi strpljiv, ali intelektualno rigorozan. Odgovaraj kratko, 2-4 rečenice."""

    if rag_tool is not None:
        instruction += "\nKoristi bazu znanja (PhilosophyKnowledgeBase) da pronađeš relevantne koncepte, ali ih preformuliraj u pitanja."

    from agents.adk_agents.adk_agent_factory import _build_model, _make_usage_callback

    _model = model or "gemini-3.5-flash"
    _kwargs = dict(
        name="Socrates",
        model=_build_model(_model, "Socrates"),
        tools=tools,
        instruction=instruction,
    )
    # Token accounting (built outside the factory, so wire the callback here too).
    if os.getenv("TOKEN_STATS_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
        _kwargs["after_model_callback"] = _make_usage_callback("Socrates", _model)

    fallback_agent = LlmAgent(**_kwargs)
    logger.info(f"Socrates agent created (model={model}, RAG={'yes' if rag_tool else 'no'})")
    return fallback_agent


# Create singleton instance for easy import
socrates_agent = None


def get_socrates_agent(
    model: str = "gemini-3-pro-preview",
    credentials=None
) -> LlmAgent:
    """
    Get or create singleton Socrates agent instance.

    Args:
        model: Gemini model
        credentials: Optional OAuth2 credentials

    Returns:
        LlmAgent instance
    """
    global socrates_agent

    if socrates_agent is None:
        socrates_agent = create_socrates_agent(
            model=model,
            credentials=credentials
        )

    return socrates_agent


if __name__ == "__main__":
    # Test agent creation
    import asyncio

    async def test():
        agent = create_socrates_agent()
        print(f"[OK] Socrates agent loaded: {agent.name}")
        print(f"   Model: {agent.model if hasattr(agent, 'model') else 'Custom Model'}")
        print(f"   Description: Socratic dialogue specialist")
        print(f"   Tools: {len(agent.tools) if hasattr(agent, 'tools') else 'Unknown'}")

        print(f"\n[CAPABILITIES] Socratic Method:")
        print("   - Teaching through questions, not answers")
        print("   - Deep philosophical reasoning")
        print("   - Philosophy knowledge base (RAG)")
        print("   - Counter-questions to challenge assumptions")
        print("   - Analogies and reductio ad absurdum")

        print(f"\n[USAGE] Keyword Triggers:")
        print("   - 'Sokrat' / 'Socrates'")
        print("   - 'filozofija' / 'philosophy'")
        print("   - Direct philosophical questions")

        print(f"\n[EXAMPLE DIALOGUE]:")
        print("   User: Sokrat, što je istina?")
        print("   Socrates: Zanimljivo pitanje! Ali reci mi prvo - kako znaš da je nešto istinito?")
        print("   User: Pa... to je očito.")
        print("   Socrates: Očito? Znači li to da je sve što ti je očito istinito? A što ako se prevaraš?")

    asyncio.run(test())
