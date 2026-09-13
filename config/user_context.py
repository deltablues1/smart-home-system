"""Request-scoped user context.

The system currently serves a single user (single Google account, single
household). This module exists so new code (briefing, meeting scheduling,
smart-home confirmation) receives an explicit ``UserContext`` instead of
reading globals — when multi-user support lands, only
``get_default_user_context()`` needs to change into a real lookup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class UserContext:
    """Identity and locale of the person a request is executed for.

    Deliberately distinct concepts (even while they all point to one person):
    - ``user_id``: who issued the request (session/channel identity)
    - ``google_account_id``: which Google account credentials act as
    - ``timezone``/``language``: formatting and scheduling locale
    - ``channel``: where the request came from (web, telegram, voice, cli)
    """

    user_id: str = "tomislav"
    google_account_id: str = "default"
    timezone: str = "Europe/Zagreb"
    language: str = "hr"
    channel: str = "web"

    def with_channel(self, channel: str) -> "UserContext":
        return replace(self, channel=channel)


def get_default_user_context(channel: str = "web") -> UserContext:
    """Return the single configured user. Env-overridable for tests/deploys."""
    return UserContext(
        user_id=os.getenv("DEFAULT_USER_ID", "tomislav"),
        google_account_id=os.getenv("DEFAULT_GOOGLE_ACCOUNT_ID", "default"),
        timezone=os.getenv("DEFAULT_USER_TIMEZONE", "Europe/Zagreb"),
        language=os.getenv("DEFAULT_USER_LANGUAGE", "hr"),
        channel=channel,
    )
