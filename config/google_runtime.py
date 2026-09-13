"""Shared runtime helpers for Gemini / Vertex configuration."""

from __future__ import annotations

import os


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def get_google_cloud_project(required: bool = False) -> str:
    """Return configured Google Cloud project ID."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if required and not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT not set")
    return project_id


def get_gemini_location(default: str = "global") -> str:
    """
    Return the preferred Gemini text/STT/TTS location.

    For Gemini runtime calls we prefer:
    1. VERTEX_AI_LOCATION when explicitly set
    2. GOOGLE_CLOUD_LOCATION when explicitly set
    3. global by default
    """
    return (
        os.getenv("VERTEX_AI_LOCATION", "").strip()
        or os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()
        or default
    )


def get_vertex_location(default: str = "us-west1") -> str:
    """
    Return the preferred regional Vertex location for non-global resources.

    Use this for RAG, media, and other region-bound services.
    """
    return (
        os.getenv("VERTEX_AI_LOCATION", "").strip()
        or os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()
        or default
    )


def get_google_api_key() -> str:
    """Return the configured Gemini/Google API key."""
    return (
        os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("GOOGLE_API_KEY", "").strip()
    )


def genai_vertex_env_keys() -> list[str]:
    """Environment keys that force google.genai into Vertex mode."""
    return [
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "VERTEX_AI_LOCATION",
    ]
