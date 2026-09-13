"""
Datetime Context Injection for ADK Agents

Centralized helper for injecting current datetime context into agent instructions.
This ensures all agents have accurate time awareness for temporal operations.

Usage:
    from agents.adk_agents.datetime_context import inject_datetime_context

    instruction = inject_datetime_context(instruction, user_timezone="Europe/Zagreb")
"""

import logging
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)

# Croatian day name translations
DAY_NAMES_CROATIAN = {
    'Monday': 'ponedjeljak',
    'Tuesday': 'utorak',
    'Wednesday': 'srijeda',
    'Thursday': 'četvrtak',
    'Friday': 'petak',
    'Saturday': 'subota',
    'Sunday': 'nedjelja'
}

# Croatian month name translations
MONTH_NAMES_CROATIAN = {
    'January': 'siječanj',
    'February': 'veljača',
    'March': 'ožujak',
    'April': 'travanj',
    'May': 'svibanj',
    'June': 'lipanj',
    'July': 'srpanj',
    'August': 'kolovoz',
    'September': 'rujan',
    'October': 'listopad',
    'November': 'studeni',
    'December': 'prosinac'
}


def inject_datetime_context(
    instruction: str,
    user_timezone: str = "Europe/Zagreb",
    log_injection: bool = True
) -> str:
    """
    Inject current datetime, date, and timezone into agent instructions.

    This ensures agents always know the current time context for:
    - Calendar scheduling (Secretary)
    - Email timestamps (Mailer)
    - Task due dates (Tracker)
    - Document dates
    - Document dating (Scribe)
    - Workflow coordination (Orchestrator)

    Replaces these placeholders in instruction text:
    - {current_datetime} -> "Friday, January 24, 2026 at 09:45 PM CET"
    - {current_date} -> "2026-01-24 (petak)"
    - {user_timezone} -> "Europe/Zagreb"

    Args:
        instruction: Base instruction text with placeholders
        user_timezone: User's timezone (default: "Europe/Zagreb")
        log_injection: Whether to log the injection (default: True)

    Returns:
        Instruction with datetime placeholders replaced

    Example:
        >>> instruction = "Today is {current_date}. Timezone: {user_timezone}"
        >>> result = inject_datetime_context(instruction)
        >>> # result: "Today is 2026-01-24 (petak). Timezone: Europe/Zagreb"
    """
    try:
        tz = pytz.timezone(user_timezone)
        current_time = datetime.now(tz)

        # Format current datetime (English for LLM understanding)
        datetime_str = current_time.strftime("%A, %B %d, %Y at %I:%M %p %Z")

        # Format current date (ISO format for easy parsing)
        date_str = current_time.strftime("%Y-%m-%d")

        # Day name in Croatian for better UX
        day_name_english = current_time.strftime("%A")
        day_name_croatian = DAY_NAMES_CROATIAN.get(day_name_english, day_name_english)

        # Enhanced date string with both formats
        date_display = f"{date_str} ({day_name_croatian})"

        # Replace placeholders
        instruction = instruction.replace("{current_datetime}", datetime_str)
        instruction = instruction.replace("{current_date}", date_display)
        instruction = instruction.replace("{user_timezone}", user_timezone)

        if log_injection:
            logger.info(f"[DATETIME] INJECTED DATETIME CONTEXT: {datetime_str}")
            logger.info(f"[DATETIME] TODAY'S DATE: {date_display}")

    except Exception as e:
        logger.warning(f"Failed to inject datetime context: {e}")
        # Fallback values
        instruction = instruction.replace("{current_datetime}", "Unknown")
        instruction = instruction.replace("{current_date}", "Unknown")
        instruction = instruction.replace("{user_timezone}", user_timezone)

    return instruction


DATETIME_PLACEHOLDERS = ("{current_datetime}", "{current_date}", "{user_timezone}")


def has_datetime_placeholders(instruction: str) -> bool:
    """True when an instruction still needs its time context filled in."""
    return any(token in instruction for token in DATETIME_PLACEHOLDERS)


def make_datetime_instruction(template: str, user_timezone: str = "Europe/Zagreb"):
    """Return an ADK instruction provider that re-renders the clock every turn.

    Substituting the time once, at agent creation, freezes it for the life of
    the process. That is invisible in a CLI run and badly wrong for a bot that
    stays up for days: on 2026-08-20 the Telegram process started at 20:21, and
    at 20:29 the scheduler agent turned "za dvije minute" into 20:25 — a time
    already in the past — because that was two minutes after the *boot* clock.

    ADK accepts a callable for `instruction` and calls it per invocation, so
    the template is kept and rendered fresh each time.
    """
    def provider(_context) -> str:
        return inject_datetime_context(template, user_timezone, log_injection=False)

    return provider


def get_datetime_context_block(user_timezone: str = "Europe/Zagreb") -> str:
    """
    Generate a datetime context block that can be prepended to instructions.

    Use this when instructions don't have placeholders - prepend this block
    to give agents time awareness.

    Args:
        user_timezone: User's timezone

    Returns:
        Markdown block with datetime context
    """
    try:
        tz = pytz.timezone(user_timezone)
        current_time = datetime.now(tz)

        datetime_str = current_time.strftime("%A, %B %d, %Y at %I:%M %p %Z")
        date_str = current_time.strftime("%Y-%m-%d")
        day_name_english = current_time.strftime("%A")
        day_name_croatian = DAY_NAMES_CROATIAN.get(day_name_english, day_name_english)
        date_display = f"{date_str} ({day_name_croatian})"

        return f"""
---

## 🕐 CURRENT DATETIME CONTEXT (CRITICAL!)

**Current Date & Time:** {datetime_str}
**Today's Date:** {date_display}
**User Timezone:** {user_timezone}

⚠️ **IMPORTANT:** Always use the above date as "today" for all temporal calculations!
- When user says "today" → Use the date shown above
- When user says "tomorrow" → Add 1 day to the date above
- When user says "next week" → Add 7 days to the date above
- NEVER assume or guess the current date - ALWAYS use the injected date above!

---

"""
    except Exception as e:
        logger.warning(f"Failed to generate datetime context block: {e}")
        return ""


# For testing
if __name__ == "__main__":
    # Test inject_datetime_context
    test_instruction = """
    # Test Agent
    Current datetime: {current_datetime}
    Today: {current_date}
    Timezone: {user_timezone}
    """

    result = inject_datetime_context(test_instruction, "Europe/Zagreb")
    print("=== inject_datetime_context ===")
    print(result)

    # Test get_datetime_context_block
    print("\n=== get_datetime_context_block ===")
    print(get_datetime_context_block("Europe/Zagreb"))
