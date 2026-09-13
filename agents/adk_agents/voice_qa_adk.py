"""
Voice QA ADK Agent

Lightweight spoken-question agent for general voice Q&A.
Designed for fast, concise answers without tools or enterprise orchestration.
"""

from typing import Optional
import logging
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    from dotenv import load_dotenv

    load_dotenv()

from google.adk.agents import LlmAgent

from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_voice_qa_agent(
    model: str = "gemini-2.5-flash",
    credentials=None,
) -> LlmAgent:
    """Create a lightweight general-purpose spoken Q&A agent."""

    instruction_file = os.path.join(
        os.path.dirname(__file__),
        "..",
        "voice_qa",
        "instructions.md",
    )

    try:
        with open(instruction_file, "r", encoding="utf-8") as f:
            instruction = f.read()
    except Exception as exc:
        logger.warning("Failed to load voice_qa instructions: %s", exc)
        instruction = (
            "You are Voice QA, a concise Croatian-speaking general knowledge assistant "
            "for spoken responses."
        )

    agent = create_adk_agent(
        name="voice_qa",
        model=model,
        description="Fast spoken Q&A specialist for general knowledge, explanations, and everyday questions without tools.",
        tools=[],
        instruction=instruction,
        load_instruction_from_file=False,
        config={
            "temperature": 0.4,
            "max_tokens": 2048,
        },
    )

    logger.info("Voice QA ADK agent created (pure LLM, no tools)")
    logger.info("Model: %s", model)
    return agent


voice_qa_agent = None


def get_voice_qa_agent(
    model: str = "gemini-2.5-flash",
    credentials=None,
) -> LlmAgent:
    """Get or create singleton Voice QA agent instance."""

    global voice_qa_agent

    if voice_qa_agent is None:
        voice_qa_agent = create_voice_qa_agent(
            model=model,
            credentials=credentials,
        )

    return voice_qa_agent

