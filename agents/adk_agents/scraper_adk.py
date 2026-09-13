"""
Scraper ADK Agent - Web Data Extraction Specialist

Native Google ADK implementation using LlmAgent.
Uses Research ADK tools for web scraping operations.
"""

import logging
from google.genai.types import Tool
from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_scraper_agent(
    model: str = "gemini-3.5-flash"
):
    """
    Create Scraper ADK agent for web data extraction.

    Scraper is a specialized agent for extracting structured data from web pages:
    - Scrape single URLs for content extraction
    - Scrape multiple URLs in batch
    - Extract structured data from HTML
    - Clean and format extracted data

    Note: This agent uses the scraping tools from research_adk_tools.py
    (scrape_url and scrape_multiple_urls) which are already ADK-compatible.

    Args:
        model: Gemini model to use (default: gemini-2.5-flash)

    Returns:
        LlmAgent configured for web scraping
    """
    # Load instruction from markdown file
    import os
    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "scraper", "instructions.md"
    )

    with open(instruction_path, "r", encoding="utf-8") as f:
        instruction = f.read()

    # Import web scraping tools from research ADK tools
    from tools.adk_tools.research_adk_tools import (
        scrape_url,
        scrape_multiple_urls
    )

    # Create list of tools
    # Note: We only use scraping tools here, not the full research toolkit
    tools = [
        scrape_url,
        scrape_multiple_urls
    ]

    # Create agent using factory
    agent = create_adk_agent(
        name="scraper",
        model=model,
        description="Web scraping specialist: extract structured data from websites, clean HTML content",
        tools=tools,
        instruction=instruction,
        config={
            "temperature": 0.3,  # Low temperature for precise data extraction
            "max_tokens": 2048,  # Higher limit for large extractions
        }
    )

    logger.info(f"Scraper agent created with {len(tools)} tools")
    return agent


# For backward compatibility and testing
if __name__ == "__main__":
    agent = create_scraper_agent()
    print(f"Scraper agent created: {agent.name}")
    print(f"Tools: {len(agent._tools)}")
