import asyncio

from config.google_runtime import get_gemini_location
from monitoring.metrics import MetricsCollector
from services import voice_fast_path
from services.google_retry import classify_google_runtime_error


def test_get_gemini_location_prefers_vertex(monkeypatch):
    monkeypatch.setenv("VERTEX_AI_LOCATION", "us-west1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    assert get_gemini_location() == "us-west1"


def test_get_gemini_location_falls_back_to_global(monkeypatch):
    monkeypatch.delenv("VERTEX_AI_LOCATION", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    assert get_gemini_location() == "global"


def test_classify_google_runtime_error_marks_quota():
    error_type, retryable, is_quota = classify_google_runtime_error(
        RuntimeError("429 RESOURCE_EXHAUSTED")
    )

    assert error_type == "quota"
    assert retryable is True
    assert is_quota is True


def test_classify_google_runtime_error_marks_billing():
    error_type, retryable, is_quota = classify_google_runtime_error(
        RuntimeError("403 PERMISSION_DENIED BILLING_DISABLED")
    )

    assert error_type == "billing"
    assert retryable is False
    assert is_quota is False


def test_metrics_collector_stores_recent_events(monkeypatch):
    monkeypatch.setenv("USE_CLOUD_LOGGING", "false")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    collector = MetricsCollector()

    collector.log_event("voice_turn_summary", {"route_hint": "smart_home"})
    collector.log_event("web_tts_request", {"success": True})

    recent = collector.get_recent_events(limit=2)

    assert len(recent) == 2
    assert recent[0]["event_type"] == "web_tts_request"
    assert collector.get_metrics()["recent_event_count"] == 2


def test_execute_fast_smart_home_command_uses_none_response(monkeypatch):
    async def fake_switch(device_name: str, state: str):
        assert device_name == "svjetlo_kuhinja"
        assert state == "ON"
        return {"status": "confirmed"}

    monkeypatch.setattr(voice_fast_path, "mqtt_switch_control", fake_switch)

    result = asyncio.run(
        voice_fast_path.execute_fast_smart_home_command(
            "Upali svjetlo u kuhinji",
            response_mode="none",
        )
    )

    assert result == ""
