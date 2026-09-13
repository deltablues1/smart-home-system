"""
Pytest Configuration and Fixtures
"""

import pytest
import os
import sys
from unittest.mock import Mock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load .env so tests use the same credentials as the app
# (GOOGLE_APPLICATION_CREDENTIALS → service account). Without this, the
# Firestore client falls back to user ADC without a quota project → 403.
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


def _suppress_grpc_teardown_noise():
    """
    Suppress gRPC + asyncio teardown noise on Windows during test teardown.

    Three hooks must be patched because gRPC raises in all three paths:
    1. sys.excepthook     — main-thread uncaught exceptions
    2. threading.excepthook — per-thread uncaught exceptions (gRPC worker threads)
    3. sys.unraisablehook — exceptions from __del__ / async teardown

    This is a known grpc+asyncio+Windows issue (grpc/grpc#25364).
    """
    import sys as _sys
    import threading as _threading

    _GRPC_MARKERS = (
        "grpc", "StatusCode", "Channel", "AbortError",
        "aiohttp", "Event loop is closed",
        # asyncio shutdown noise
        "Task was destroyed but it is pending",
    )

    def _is_grpc_noise(val) -> bool:
        # Wrapped in try/except — during interpreter shutdown str() on a dead object can raise.
        try:
            msg = str(val) if val is not None else ""
            return any(m.lower() in msg.lower() for m in _GRPC_MARKERS)
        except Exception:
            return True  # If we can't inspect it, suppress it (shutdown noise)

    # 1. sys.excepthook (main thread)
    # IMPORTANT: this function must NEVER raise — Python C code prints
    # "Error in sys.excepthook:" if it does, which is the noise we're suppressing.
    _orig_excepthook = _sys.excepthook

    def _excepthook(exc_type, exc_val, exc_tb):
        try:
            if _is_grpc_noise(exc_val):
                return
            _orig_excepthook(exc_type, exc_val, exc_tb)
        except Exception:
            pass  # Swallow hook failure — never let this propagate

    _sys.excepthook = _excepthook

    # 2. threading.excepthook (gRPC background threads)
    _orig_threading_hook = _threading.excepthook

    def _threading_excepthook(args):
        try:
            if _is_grpc_noise(args.exc_value):
                return
            _orig_threading_hook(args)
        except Exception:
            pass

    _threading.excepthook = _threading_excepthook

    # 3. sys.unraisablehook (__del__ / async finaliser noise)
    _orig_unraisable = _sys.unraisablehook

    def _unraisablehook(unraisable):
        try:
            if _is_grpc_noise(unraisable.exc_value):
                return
            _orig_unraisable(unraisable)
        except Exception:
            pass

    _sys.unraisablehook = _unraisablehook


_suppress_grpc_teardown_noise()


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Setup test environment variables"""
    os.environ['ENVIRONMENT'] = 'test'
    os.environ['LOG_LEVEL'] = 'DEBUG'
    yield

@pytest.fixture
def mock_orchestrator():
    """Mock orchestrator fixture - returns ADK orchestrator agent"""
    from agents.adk_agents.orchestrator_adk import create_orchestrator_agent
    return create_orchestrator_agent(sub_agents=[])

@pytest.fixture
def sample_markdown():
    return """# Heading 1\n**bold** and *italic*"""

@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """Keep every test's job bookkeeping in its own directory.

    These stores are written by ordinary code paths under test — a deferred
    voice request records a job, a scheduled run records its outcome — so
    without this the suite scribbles real files into the working tree.
    """
    monkeypatch.setenv("BACKGROUND_JOBS_FILE", str(tmp_path / "background_jobs.json"))
    monkeypatch.setenv("SCHEDULER_RESULTS_FILE", str(tmp_path / "job_results.json"))

    import config.scheduler_config as scheduler_config
    monkeypatch.setattr(
        scheduler_config, "RESULTS_FILE", tmp_path / "job_results.json"
    )

    # The reservation table is in-process state, so it leaks between tests
    # just as the files would.
    from services import background_jobs
    background_jobs.reset()
    try:
        yield
    finally:
        background_jobs.reset()


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow running tests")
