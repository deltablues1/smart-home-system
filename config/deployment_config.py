"""
Deployment profile and feature flag configuration.

Profiles:
  - "dev"      : Local development (all features, no API token)
  - "web"      : Web/server deployment (all features)
  - "rpi-home" : Raspberry Pi smart-home runtime — wake word + selected
                 Google Workspace + conversational agents.

Environment variables (override profile defaults):
  DEPLOYMENT_PROFILE     - "dev" (default), "web" or "rpi-home"
  TELEGRAM_ENABLED       - force Telegram interface on/off
  WAKE_WORD_ENABLED      - enable the wake word listener
  VOICE_MODE_DEFAULT     - "agent" or "live"
  API_TOKEN_REQUIRED     - require bearer token on /api/*
  VOICE_DIRECT_ROUTING   - direct-route wake-word requests to home/christian/socrates
  DISABLE_ADK_TELEMETRY  - disable flaky ADK tracing that can crash on bytes payloads
  ENABLE_WEB_SCHEDULER   - allow web process to start embedded APScheduler
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Dict


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    return value or default


@dataclass(frozen=True)
class DeploymentConfig:
    profile: str
    telegram_enabled: bool
    wake_word_enabled: bool
    voice_mode_default: str
    api_token_required: bool
    voice_direct_routing: bool
    adk_telemetry_disabled: bool
    web_scheduler_enabled: bool

    def to_public_dict(self) -> Dict[str, object]:
        return asdict(self)


_DEFAULTS = {
    "dev": {
        "telegram_enabled": True,
        "wake_word_enabled": False,
        "voice_mode_default": "agent",
        "api_token_required": False,
        "voice_direct_routing": False,
        "adk_telemetry_disabled": False,
        "web_scheduler_enabled": True,
    },
    "web": {
        "telegram_enabled": True,
        "wake_word_enabled": False,
        "voice_mode_default": "agent",
        "api_token_required": False,
        "voice_direct_routing": False,
        "adk_telemetry_disabled": False,
        "web_scheduler_enabled": True,
    },
    "rpi-home": {
        "telegram_enabled": True,
        "wake_word_enabled": True,
        "voice_mode_default": "agent",
        "api_token_required": True,
        "voice_direct_routing": True,
        "adk_telemetry_disabled": True,
        "web_scheduler_enabled": False,
    },
}

# The RPi home profile loads a deliberate subset of the registry.
# Keep it focused on smart-home, Google Workspace, research, and conversational use.
RPI_HOME_ALLOWED_AGENTS = {
    "orchestrator",
    "decision_validator",
    "ask_user",
    "mailer",
    "librarian",
    "analyst",
    "secretary",
    "rolodex",
    "tracker",
    "researcher",
    "scribe",
    "scraper",
    "synthesizer",
    "voice_qa",
    "socrates",
    "christian_guide",
    "smart_home",
    # Lets the user say "napravi X u 21h" from Telegram/voice; the job is
    # persisted and executed by the adk-scheduler daemon, not in this process.
    "scheduler",
}

# Agents that are loaded and routable, but must never be CALLED by an
# execution path. voice_qa sits in front of the orchestrator on the voice lane:
# it has no tools, and when a request needs one it answers with the
# [[ESCALATE]] sentinel that only base_interface knows how to unwrap -- and only
# when voice_qa was the routed agent. Called as a tool, or scheduled as a
# plan-execute step, that sentinel is just text nobody acts on.
#
# It stays in get_worker_agent_names() because the voice lane looks it up in
# system.worker_agents. Both execution paths filter it out through here, so a
# future addition cannot be closed on one path and left open on the other.
NON_CALLABLE_WORKER_AGENTS = frozenset({"voice_qa"})


def callable_worker_agents(worker_agents):
    """The subset of loaded workers an execution path may invoke."""
    return [
        a for a in worker_agents
        if getattr(a, "name", "") not in NON_CALLABLE_WORKER_AGENTS
    ]


_deployment_config: DeploymentConfig | None = None


def reset_deployment_config_cache() -> None:
    global _deployment_config
    _deployment_config = None


def get_deployment_config() -> DeploymentConfig:
    global _deployment_config
    if _deployment_config is not None:
        return _deployment_config

    profile = _env_str("DEPLOYMENT_PROFILE", "dev")
    if profile not in _DEFAULTS:
        # Falling back to "dev" on a typo is how a deployment meant to require
        # authentication quietly stops requiring it. The profile decides that,
        # so an unrecognised one is a configuration error, not a default.
        raise RuntimeError(
            f"Unknown DEPLOYMENT_PROFILE {profile!r}. "
            f"Valid profiles: {', '.join(sorted(_DEFAULTS))}. "
            "Refusing to start rather than silently using development settings."
        )
    defaults = _DEFAULTS[profile]
    voice_mode_default = _env_str(
        "VOICE_MODE_DEFAULT",
        defaults["voice_mode_default"],
    ).lower()
    if voice_mode_default not in {"agent", "live"}:
        voice_mode_default = defaults["voice_mode_default"]

    _deployment_config = DeploymentConfig(
        profile=profile,
        telegram_enabled=_env_bool("TELEGRAM_ENABLED", defaults["telegram_enabled"]),
        wake_word_enabled=_env_bool("WAKE_WORD_ENABLED", defaults["wake_word_enabled"]),
        voice_mode_default=voice_mode_default,
        api_token_required=_env_bool("API_TOKEN_REQUIRED", defaults["api_token_required"]),
        voice_direct_routing=_env_bool("VOICE_DIRECT_ROUTING", defaults["voice_direct_routing"]),
        adk_telemetry_disabled=_env_bool("DISABLE_ADK_TELEMETRY", defaults["adk_telemetry_disabled"]),
        web_scheduler_enabled=_env_bool("ENABLE_WEB_SCHEDULER", defaults["web_scheduler_enabled"]),
    )
    return _deployment_config


def is_telegram_enabled() -> bool:
    return get_deployment_config().telegram_enabled


def is_wake_word_enabled() -> bool:
    return get_deployment_config().wake_word_enabled


def is_api_token_required() -> bool:
    return get_deployment_config().api_token_required


def is_rpi_home() -> bool:
    return get_deployment_config().profile == "rpi-home"


def is_voice_direct_routing() -> bool:
    return get_deployment_config().voice_direct_routing


def is_adk_telemetry_disabled() -> bool:
    return get_deployment_config().adk_telemetry_disabled


def is_web_scheduler_enabled() -> bool:
    return get_deployment_config().web_scheduler_enabled


def get_allowed_agent_names(all_names: "list[str] | None" = None) -> "set[str] | None":
    """Agent whitelist for the active profile, or None when unrestricted."""
    if is_rpi_home():
        return set(RPI_HOME_ALLOWED_AGENTS)
    return None
