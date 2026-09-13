"""
Christian Guide ADK Agent

Reflective Christian / spiritual guide with optional Vertex AI RAG corpus.
"""

import logging
import os

from google.adk.agents import LlmAgent

logger = logging.getLogger(__name__)


def create_christian_guide_agent(
    model: str = "gemini-3-flash-preview",
    credentials=None,
) -> LlmAgent:
    """
    Create a Christian guide agent for reflective dialogue, discernment,
    doctrine-aware explanations, and guided spiritual exercises.
    """
    from tools.christian_tools import get_christian_rag_tool

    rag_tool = get_christian_rag_tool()
    tools = [rag_tool] if rag_tool is not None else []

    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "christian_guide", "instructions.md"
    )
    with open(instruction_path, "r", encoding="utf-8") as handle:
        instruction = handle.read()

    if rag_tool is not None:
        instruction += (
            "\nKoristi ChristianKnowledgeBase kad korisnik pita o Bibliji, "
            "krscanskom nauku, duhovnim klasicima, razlucivanju, molitvi ili "
            "duhovnim vjezbama. Kada koristis bazu znanja, oslanjaj se na "
            "izvore, ali odgovori formuliraj prirodno i razgovorno."
        )

    from agents.adk_agents.adk_agent_factory import (
        _append_untrusted_content_rule,
        _build_model,
        _make_usage_callback,
    )

    # The RAG corpus is external content: a document in it can carry text that
    # reads like an instruction. This agent is built here rather than through
    # create_adk_agent, so the boundary every other content-reading agent gets
    # for free has to be applied explicitly.
    instruction = _append_untrusted_content_rule("christian_guide", instruction)

    _model = model or "gemini-3-flash-preview"
    _kwargs = dict(
        name="christian_guide",
        model=_build_model(_model, "christian_guide"),
        tools=tools,
        instruction=instruction,
        description=(
            "Christian spiritual guide for doctrine-aware explanations, "
            "reflection, discernment, prayer, and guided exercises"
        ),
    )
    # Token accounting (built outside the factory, so wire the callback here too).
    if os.getenv("TOKEN_STATS_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
        _kwargs["after_model_callback"] = _make_usage_callback("christian_guide", _model)

    agent = LlmAgent(**_kwargs)
    logger.info(
        "christian_guide agent created (model=%s, RAG=%s)",
        model,
        "yes" if rag_tool else "no",
    )
    return agent


christian_guide_agent = None


def get_christian_guide_agent(
    model: str = "gemini-3-flash-preview",
    credentials=None,
) -> LlmAgent:
    """Get or create singleton christian_guide agent instance."""
    global christian_guide_agent

    if christian_guide_agent is None:
        christian_guide_agent = create_christian_guide_agent(
            model=model,
            credentials=credentials,
        )

    return christian_guide_agent
