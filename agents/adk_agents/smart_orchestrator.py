"""
Smart Orchestrator - Multi-Agent Coordinator

Coordinates 14 specialist agents using Google ADK's AgentTool pattern.
Worker agents are wrapped as tools (called like functions), ensuring
reliable sequential execution of multi-step workflows.

Usage:
    orchestrator = create_smart_orchestrator(
        worker_agents=[researcher, mailer, librarian, ...],
    )
"""

import os
import logging
from typing import List, Optional
from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.genai import types
from config.deployment_config import callable_worker_agents

logger = logging.getLogger(__name__)

# Minimal fallback instruction (used only if instructions.md fails to load)
FALLBACK_INSTRUCTION = (
    "You are the Smart Orchestrator. You coordinate specialist agents "
    "to fulfill user requests. Call agents as tools sequentially, pass "
    "results between them, verify each step, and return a complete summary. "
    "When user mentions a file by name, use librarian first to get the file ID, "
    "then pass the ID to analyst. When user mentions a person by name, use "
    "rolodex first to get the email, then pass it to mailer."
)


def create_smart_orchestrator(
    model: str = "gemini-3.5-flash",
    worker_agents: Optional[List[LlmAgent]] = None,
    validator_agent: Optional[LlmAgent] = None,
    ask_user_agent: Optional[LlmAgent] = None
) -> LlmAgent:
    """
    Create Smart Orchestrator with AgentTool pattern.

    Worker agents are wrapped as AgentTools (called like functions).
    Validator and Ask User remain as sub_agents (transfer_to_agent pattern).

    Args:
        model: Model to use (default: gemini-2.5-pro)
        worker_agents: Specialist agents to wrap as tools
        validator_agent: Optional decision validator (sub_agent)
        ask_user_agent: Optional ask user handler (sub_agent)

    Returns:
        Smart Orchestrator LlmAgent instance
    """
    from agents.adk_agents.adk_agent_factory import (
        claude_safe_generation_kwargs,
        create_adk_agent,
    )
    from agents.adk_agents.control_callbacks import validate_worker_result

    # --- Load instructions from file ---
    instruction_file = os.path.join(
        os.path.dirname(__file__), "..", "orchestrator", "instructions.md"
    )

    try:
        with open(instruction_file, 'r', encoding='utf-8') as f:
            instruction = f.read()
            logger.info("Loaded orchestrator instructions from instructions.md")
    except Exception as e:
        logger.warning(f"Failed to load instruction file: {e}, using fallback")
        instruction = FALLBACK_INSTRUCTION

    # --- Wrap worker agents as AgentTools ---
    agent_tools = []
    worker_list_parts = []

    if worker_agents:
        # NON_CALLABLE_WORKER_AGENTS is shared with plan-execute: a worker that
        # must not be invoked has to be unreachable on BOTH execution paths.
        for agent in callable_worker_agents(worker_agents):
            # skip_summarization=False lets orchestrator generate its own response
            # after receiving tool results (language adaptation, summary, next steps)
            agent_tool = AgentTool(agent=agent, skip_summarization=False)
            agent_tools.append(agent_tool)
            worker_list_parts.append(f"- **{agent.name}**: {agent.description}")
        logger.info(f"Wrapped {len(agent_tools)} worker agents as AgentTools (skip_summarization=False)")

    worker_list = "\n".join(worker_list_parts) if worker_list_parts else "No worker agents configured"

    # --- Sub-agents (validator + ask_user) ---
    special_sub_agents = []
    validator_desc = ""
    ask_user_desc = ""

    if validator_agent:
        special_sub_agents.append(validator_agent)
        validator_desc = f"**{validator_agent.name}**: {validator_agent.description}"

    if ask_user_agent:
        special_sub_agents.append(ask_user_agent)
        ask_user_desc = f"**{ask_user_agent.name}**: {ask_user_agent.description}"

    # --- Replace placeholders ---
    instruction = instruction.replace("{WORKER_AGENTS}", worker_list)
    instruction = instruction.replace("{VALIDATOR_AGENT}", validator_desc)
    instruction = instruction.replace("{ASK_USER_AGENT}", ask_user_desc)

    # Inject current datetime context
    # Datum/vrijeme se NE ubacuje ovdje: to bi zamrznulo sat na trenutak
    # kad je agent stvoren. Predaje se predložak, a tvornica ga omota u
    # ADK instruction provider koji ga renderira pri svakom pozivu.

    # --- Create orchestrator ---
    orchestrator = create_adk_agent(
        name="smart_orchestrator",
        model=model,
        description="Coordinates specialist agents to fulfill user requests via sequential tool calls",
        tools=agent_tools,
        sub_agents=special_sub_agents,
        instruction=instruction,
        load_instruction_from_file=False,  # Already loaded above
        # Guardrail: block silent empty/failed worker results from propagating.
        after_tool_callback=validate_worker_result,
    )

    # Set generation config properly (not via _config which has no effect)
    orchestrator.generate_content_config = types.GenerateContentConfig(
        **claude_safe_generation_kwargs(
            "smart_orchestrator",
            model,
            temperature=0.2,  # Low for deterministic routing decisions
            max_output_tokens=8192,
        )
    )

    logger.info(
        f"Smart Orchestrator created: {len(worker_agents or [])} workers, "
        f"validator={bool(validator_agent)}, ask_user={bool(ask_user_agent)}"
    )
    return orchestrator
