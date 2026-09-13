"""
Metrics must not grow for as long as the process runs.

record_timing appended to an unbounded list, so every timed call ever made was
kept in memory to compute an average — on a Pi that stays up for weeks, that is
a slow leak with no upper bound.

Replacing the list with a bounded buffer alone would have been worse than the
leak: "count" would silently have become "count since the buffer last filled",
and a metric that lies is harder to notice than one that grows. Count, min and
max are tracked separately and stay lifetime figures.

Run with:
    pytest tests/unit/test_metrics_memory.py -v
"""

import pytest

from monitoring.metrics import MetricsCollector


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setenv("METRICS_TIMING_WINDOW", "50")
    # Every record_timing also ships an event to Cloud Logging. Left enabled,
    # this test would be measuring the network rather than the buffer.
    monkeypatch.setenv("USE_CLOUD_LOGGING", "false")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    collector = MetricsCollector()
    collector.cloud_logger = None
    return collector


class TestMemoryIsBounded:
    def test_the_buffer_stops_growing(self, collector):
        for i in range(500):
            collector.record_timing("agent_call", i * 0.001)
        assert len(collector.timings["agent_call"]) == 50

    def test_a_tiny_window_is_floored(self, monkeypatch):
        monkeypatch.setenv("METRICS_TIMING_WINDOW", "1")
        assert MetricsCollector().timing_window == 20

    def test_a_garbage_window_is_a_startup_error_not_a_silent_default(self, monkeypatch):
        """Better to fail loudly at startup than to run for weeks with a buffer
        size nobody chose."""
        monkeypatch.setenv("METRICS_TIMING_WINDOW", "nonsense")
        monkeypatch.setenv("USE_CLOUD_LOGGING", "false")
        with pytest.raises(ValueError):
            MetricsCollector()


class TestTheNumbersKeepMeaningWhatTheyMeant:
    def test_count_is_the_lifetime_total_not_the_window(self, collector):
        for i in range(500):
            collector.record_timing("agent_call", 0.1)
        assert collector.get_metrics()["timings"]["agent_call"]["count"] == 500

    def test_min_and_max_survive_falling_out_of_the_window(self, collector):
        collector.record_timing("agent_call", 99.0)   # the slowest call ever
        for _ in range(200):                           # pushes it out of the window
            collector.record_timing("agent_call", 0.5)

        stats = collector.get_metrics()["timings"]["agent_call"]
        assert stats["max"] == 99.0
        assert stats["min"] == 0.5

    def test_the_average_says_what_it_averaged(self, collector):
        for _ in range(500):
            collector.record_timing("agent_call", 0.2)
        stats = collector.get_metrics()["timings"]["agent_call"]
        assert stats["avg"] == pytest.approx(0.2)
        assert stats["avg_over_last"] == 50

    def test_reset_clears_the_lifetime_figures_too(self, collector):
        collector.record_timing("agent_call", 1.0)
        collector.reset()
        collector.record_timing("agent_call", 2.0)
        stats = collector.get_metrics()["timings"]["agent_call"]
        assert stats["count"] == 1
        assert stats["max"] == 2.0
