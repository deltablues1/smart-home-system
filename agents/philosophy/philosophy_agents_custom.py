"""
Philosophy Classroom Agents - Using Custom BaseAgent
"""

from agents.base_agent import BaseAgent
from pathlib import Path
import os

# Get the directory of this file
current_dir = Path(__file__).parent

# Socrates Agent - The Teacher
# Uses Gemini 2.5 Pro for deep philosophical reasoning
socrates_agent = BaseAgent(
    name="Socrates",
    model="publishers/google/models/gemini-2.5-pro",
    instruction_file=str(current_dir / "socrates_instructions.md"),
    config={
        "temperature": 0.7
    }
)

# Termination Checker - The Moderator
# Uses Gemini 2.5 Flash for speed
termination_checker = BaseAgent(
    name="TerminationChecker",
    model="publishers/google/models/gemini-2.5-flash",
    instruction_file=str(current_dir / "termination_checker_instructions.md"),
    config={
        "temperature": 0.1
    }
)
