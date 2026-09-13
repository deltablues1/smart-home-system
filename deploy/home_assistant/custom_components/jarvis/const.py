"""Constants for the Jarvis conversation integration."""

DOMAIN = "jarvis"

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_USER_ID = "user_id"
CONF_TIMEOUT = "timeout"

# Jarvis runs on the second Raspberry Pi; the web API is the same one the
# dashboard uses. Only the config flow's pre-filled value -- the user confirms
# or replaces it when adding the integration.
DEFAULT_URL = "http://jarvis.local:8000"
DEFAULT_USER_ID = "ha-assist"

# Simple questions come back in a few seconds, but a real task can run for
# minutes: measured 2026-08-23, "research heat pumps, write it up and mail it to
# me" took ~3 minutes across 12 tool calls and finished successfully -- while a
# 90 s timeout had already told the user it had failed. The cost of waiting too
# long is a slow answer; the cost of waiting too little is lying about a task
# that actually completed.
DEFAULT_TIMEOUT = 300

# How long a pause may last before Assist decides the sentence is over. Home
# Assistant's own default is 0.7 s, which cuts people off mid-thought; Jarvis's
# wake loop on the Pi waits 1.2 s. Three seconds is what the household asked
# for -- the price is that every answer arrives that much later, because a pause
# cannot be told apart from an ending until it has lasted long enough.
CONF_SILENCE_SECONDS = "silence_seconds"
DEFAULT_SILENCE_SECONDS = 3.0

# The hard cap on one spoken turn. Home Assistant's default is 15 s, which with
# a 3 s pause allowance would leave only about twelve seconds of actual speech
# -- a new cliff right where the long sentences start.
CONF_MAX_TURN_SECONDS = "max_turn_seconds"
DEFAULT_MAX_TURN_SECONDS = 30.0

# Reopening the Assist dialog hands us a brand new conversation_id, so without
# this every turn would start a fresh Jarvis session and "a ugasi i ono drugo"
# would have nothing to refer back to. Within this window a new conversation
# continues the last one instead.
CONF_SESSION_GRACE_MINUTES = "session_grace_minutes"
DEFAULT_SESSION_GRACE_MINUTES = 10.0

# The Assist dialog forgets everything the moment it closes -- an answer can be
# gone before it has been read. Turns are kept here so they can be read later
# from a dashboard, and survive a restart.
HISTORY_STORED_TURNS = 25
HISTORY_SHOWN_TURNS = 10
# Attributes above ~16 kB are refused by the recorder, so long answers are
# shortened for the entity while the stored copy stays whole.
HISTORY_ANSWER_CHARS = 1000

SIGNAL_HISTORY_UPDATED = "jarvis_history_updated"
