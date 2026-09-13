"""
Rolodex ADK Agent - Google Contacts Specialist

Native Google ADK implementation using LlmAgent.
Uses Contacts ADK tools for Google People API operations.
"""

import logging
from google.genai.types import Tool
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_rolodex_agent(
    model: str = "gemini-3.5-flash"
):
    """
    Create Rolodex ADK agent for Google Contacts management.

    Rolodex is a specialized agent for managing Google Contacts:
    - Search contacts by name, email, or phone
    - Get contact details
    - Find email addresses for people
    - List all contacts
    - Create new contacts

    Args:
        model: Gemini model to use (default: gemini-2.5-flash)

    Returns:
        LlmAgent configured for contact management
    """
    # Load instruction from markdown file
    import os
    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "rolodex", "instructions.md"
    )

    with open(instruction_path, "r", encoding="utf-8") as f:
        instruction = f.read()

    # Import Contacts ADK tools
    from tools.adk_tools.contacts_adk_tools import (
        contacts_search_people,
        contacts_list_all,
        contacts_get_by_name,
        contacts_create_contact,
        contacts_update_contact,
        contacts_delete_contact
    )

    # Create list of tools
    tools = [
        contacts_search_people,
        contacts_list_all,
        contacts_get_by_name,
        contacts_create_contact,
        contacts_update_contact,
        contacts_delete_contact,
    ]
    description = "Google Contacts specialist: search contacts, find emails, manage address book"

    # Create agent using factory
    agent = create_adk_agent(
        name="rolodex",
        model=model,
        description=description,
        tools=tools,
        instruction=instruction,
        config={
            "temperature": 0.2,  # Low temperature for accurate data handling
            "max_tokens": 1024,
        }
    )

    logger.info(f"Rolodex agent created with {len(tools)} tools")
    return agent


# For backward compatibility and testing
if __name__ == "__main__":
    agent = create_rolodex_agent()
    print(f"Rolodex agent created: {agent.name}")
    print(f"Tools: {len(agent._tools)}")
