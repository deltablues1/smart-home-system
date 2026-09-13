"""
Tracker ADK Agent - Google Tasks Specialist

Native Google ADK implementation using LlmAgent.
Uses Tasks ADK tools for Google Tasks API operations.
"""

import logging
import os
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_tracker_agent(
    model: str = "gemini-3.5-flash",
    user_timezone: str = "Europe/Zagreb"
):
    """
    Create Tracker ADK agent for Google Tasks management.

    Tracker is a specialized agent for managing Google Tasks:
    - List task lists and tasks
    - Create new tasks with due dates and notes
    - Update tasks (modify details, mark as complete)
    - Delete tasks
    - Organize tasks into lists

    Args:
        model: Gemini model to use (default: gemini-2.5-flash)
        user_timezone: User's timezone for date awareness (default: "Europe/Zagreb")

    Returns:
        LlmAgent configured for task management
    """
    # Load instruction from markdown file
    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "tracker", "instructions.md"
    )

    with open(instruction_path, "r", encoding="utf-8") as f:
        instruction = f.read()

    # Inject current datetime context for accurate due date handling
    # Datum/vrijeme se NE ubacuje ovdje: to bi zamrznulo sat na trenutak
    # kad je agent stvoren. Predaje se predložak, a tvornica ga omota u
    # ADK instruction provider koji ga renderira pri svakom pozivu.

    # Import Tasks ADK tools
    from tools.adk_tools.tasks_adk_tools import (
        tasks_list_task_lists,
        tasks_list_tasks,
        tasks_create_task,
        tasks_update_task,
        tasks_delete_task,
        tasks_complete_task
    )

    # Create list of tools
    tools = [
        tasks_list_task_lists,
        tasks_list_tasks,
        tasks_create_task,
        tasks_update_task,
        tasks_delete_task,
        tasks_complete_task,
    ]
    description = "Google Tasks specialist for task and task-list management"

    # Create agent using factory
    agent = create_adk_agent(
        name="tracker",
        model=model,
        description=description,
        tools=tools,
        instruction=instruction,
        config={
            "temperature": 0.3,  # Slightly higher for natural task organization
            "max_tokens": 1024,
        }
    )

    logger.info(f"Tracker agent created with {len(tools)} tools")
    return agent


# For backward compatibility and testing
if __name__ == "__main__":
    agent = create_tracker_agent()
    print(f"Tracker agent created: {agent.name}")
    print(f"Tools: {len(agent._tools)}")
