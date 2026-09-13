"""
Unit tests for Phase A3 hardening:

- Telegram webhook rejects updates without the secret token
- scheduler save_jobs is atomic (no .tmp leftovers, valid JSON)
- date-trigger display no longer renders "Nones"
- circuit breaker ignores caller errors (401/403/validation), counts 5xx
- invalidates_cache clears the service cache after successful mutations

Run with:
    pytest tests/unit/test_phase_a3_hardening.py -v
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Telegram webhook secret
# ---------------------------------------------------------------------------

class TestTelegramWebhookSecret:
    def _make_interface(self):
        from interfaces.telegram_interface import TelegramInterface

        iface = object.__new__(TelegramInterface)
        iface.application = MagicMock()
        iface.application.process_update = AsyncMock()
        iface.application.bot = MagicMock()
        return iface

    def test_rejects_missing_secret(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "topsecret")
        iface = self._make_interface()

        with pytest.raises(PermissionError):
            asyncio.run(iface.process_webhook_update({"update_id": 1}))
        iface.application.process_update.assert_not_called()

    def test_rejects_wrong_secret(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "topsecret")
        iface = self._make_interface()

        with pytest.raises(PermissionError):
            asyncio.run(
                iface.process_webhook_update(
                    {"update_id": 1}, secret_header="wrong"
                )
            )
        iface.application.process_update.assert_not_called()

    def test_accepts_correct_secret(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "topsecret")
        iface = self._make_interface()

        asyncio.run(
            iface.process_webhook_update(
                {"update_id": 1}, secret_header="topsecret"
            )
        )
        iface.application.process_update.assert_called_once()

    def test_no_secret_configured_accepts(self, monkeypatch):
        # Without configuration we can't verify — behavior stays permissive
        # but a warning is logged at set_webhook time.
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        iface = self._make_interface()

        asyncio.run(iface.process_webhook_update({"update_id": 1}))
        iface.application.process_update.assert_called_once()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class TestSchedulerConfig:
    def test_save_jobs_atomic(self, tmp_path):
        from config.scheduler_config import (
            JobTrigger, ScheduledJob, SchedulerConfig, save_jobs,
        )

        target = tmp_path / "jobs.json"
        config = SchedulerConfig(jobs=[
            ScheduledJob(
                id="j1", name="Test", agent_request="do something",
                trigger=JobTrigger(type="interval", interval_seconds=60),
            )
        ])

        save_jobs(config, file_path=target)

        assert target.exists()
        assert not (tmp_path / "jobs.json.tmp").exists()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["jobs"][0]["id"] == "j1"

    def test_date_trigger_description_not_nones(self):
        from config.scheduler_config import JobTrigger
        from interfaces.scheduler_interface import SchedulerInterface

        date_trigger = JobTrigger(type="date", run_date="2026-07-15T09:00:00")
        desc = SchedulerInterface._describe_trigger(date_trigger)
        assert desc == "2026-07-15T09:00:00"
        assert "None" not in desc

        interval_trigger = JobTrigger(type="interval", interval_seconds=300)
        assert SchedulerInterface._describe_trigger(interval_trigger) == "300s"

        cron_trigger = JobTrigger(type="cron", cron_expression="0 7 * * *")
        assert SchedulerInterface._describe_trigger(cron_trigger) == "0 7 * * *"


# ---------------------------------------------------------------------------
# Circuit breaker: caller errors don't open the circuit
# ---------------------------------------------------------------------------

class _FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = MagicMock()
        self.resp.status = status


class TestCircuitBreakerErrorFiltering:
    def _breaker(self):
        from tools.resilience.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig,
        )
        return CircuitBreaker(
            "test_service", CircuitBreakerConfig(failure_threshold=2)
        )

    def test_403_does_not_count(self):
        breaker = self._breaker()

        async def failing():
            raise _FakeHttpError(403)

        for _ in range(5):
            with pytest.raises(_FakeHttpError):
                asyncio.run(breaker.call(failing))

        assert breaker.failure_count == 0
        assert breaker.state.value == "closed"

    def test_500_counts_and_opens(self):
        breaker = self._breaker()

        async def failing():
            raise _FakeHttpError(500)

        for _ in range(2):
            with pytest.raises(_FakeHttpError):
                asyncio.run(breaker.call(failing))

        assert breaker.state.value == "open"

    def test_connection_error_counts(self):
        breaker = self._breaker()

        async def failing():
            raise ConnectionError("refused")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                asyncio.run(breaker.call(failing))

        assert breaker.state.value == "open"

    def test_value_error_does_not_count(self):
        breaker = self._breaker()

        async def failing():
            raise ValueError("bad input")

        for _ in range(5):
            with pytest.raises(ValueError):
                asyncio.run(breaker.call(failing))

        assert breaker.state.value == "closed"


# ---------------------------------------------------------------------------
# Cache invalidation after mutations
# ---------------------------------------------------------------------------

class TestInvalidatesCache:
    def test_success_invalidates(self, monkeypatch):
        import tools.resilience.cache as cache_mod

        manager = MagicMock()
        monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)

        @cache_mod.invalidates_cache("calendar")
        async def mutate():
            return {"status": "created"}

        asyncio.run(mutate())
        manager.invalidate_service.assert_called_once_with("calendar")

    def test_error_result_does_not_invalidate(self, monkeypatch):
        import tools.resilience.cache as cache_mod

        manager = MagicMock()
        monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)

        @cache_mod.invalidates_cache("calendar")
        async def mutate():
            return {"status": "error", "error": "boom"}

        asyncio.run(mutate())
        manager.invalidate_service.assert_not_called()

    def test_exception_does_not_invalidate(self, monkeypatch):
        import tools.resilience.cache as cache_mod

        manager = MagicMock()
        monkeypatch.setattr(cache_mod, "get_cache_manager", lambda: manager)

        @cache_mod.invalidates_cache("calendar")
        async def mutate():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            asyncio.run(mutate())
        manager.invalidate_service.assert_not_called()
