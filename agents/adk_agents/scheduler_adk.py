"""
Scheduler ADK Agent

Native ADK implementation for managing scheduled/recurring tasks.
Uses APScheduler through scheduler ADK tools.
"""

import logging
import os
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_scheduler_agent(
    model: str = "gemini-3.5-flash",
    user_timezone: str = "Europe/Zagreb"
):
    """
    Create Scheduler ADK agent for managing recurring tasks.

    Capabilities:
    - Schedule recurring agent requests (cron, interval, one-time)
    - List, pause, resume, remove scheduled jobs
    - Natural language schedule interpretation

    Args:
        model: Gemini model to use
        user_timezone: User's timezone

    Returns:
        LlmAgent configured for scheduling
    """
    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "scheduler", "instructions.md"
    )

    with open(instruction_path, "r", encoding="utf-8") as f:
        instruction = f.read()

    # Datum/vrijeme se NE ubacuje ovdje: to bi zamrznulo sat na trenutak
    # kad je agent stvoren. Predaje se predložak, a tvornica ga omota u
    # ADK instruction provider koji ga renderira pri svakom pozivu.

    from tools.adk_tools.scheduler_adk_tools import get_scheduler_adk_tools
    tools = get_scheduler_adk_tools()

    agent = create_adk_agent(
        name="scheduler",
        model=model,
        description="Manages scheduled/recurring tasks: create, list, pause, resume, delete scheduled agent jobs",
        tools=tools,
        instruction=instruction,
        config={
            "temperature": 0.3,
            "max_tokens": 1536,
        }
    )

    logger.info(f"Scheduler ADK agent created with {len(tools)} tools")
    return agent


if __name__ == "__main__":
    agent = create_scheduler_agent()
    print(f"Scheduler agent created: {agent.name}")
