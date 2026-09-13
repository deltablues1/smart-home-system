"""
Measuring something must not slow the thing being measured.

`record_timing` called Cloud Logging synchronously, one network round trip per
measurement, on the caller's thread. Measured on 2026-09-05: a test taking 500
timings ran in 8 min 38 s with USE_CLOUD_LOGGING on and 1.8 s with it off.

The cost landed at the worst moment. Cloud logging gets switched on precisely
when someone is investigating a problem, so the instrument slowed down exactly
the thing under investigation.

Entries now go to a queue and a worker thread sends them in batches. Two
properties matter and both are asserted here: the caller never waits, and a
metric never breaks or blocks the app — a full queue drops entries and counts
them rather than holding anyone up.

No Google Cloud: the logger is a double.

Run with:
    pytest tests/unit/test_metrics_cloud_batching.py -v
"""

import queue
import threading
import time

import pytest

from monitoring.metrics import MetricsCollector


class _SlowCloudLogger:
    """Every send costs 20 ms, like a real round trip."""

    def __init__(self):
        self.entries = []
        self.batches = 0
        self._lock = threading.Lock()

    def log_struct(self, payload):
        time.sleep(0.02)
        with self._lock:
            self.entries.append(payload)

    def batch(self):
        outer = self

        class _Batch:
            def __enter__(self_inner):
                return self_inner

            def log_struct(self_inner, payload):
                with outer._lock:
                    outer.entries.append(payload)

            def __exit__(self_inner, *exc):
                time.sleep(0.02)
                with outer._lock:
                    outer.batches += 1
                return False

        return _Batch()


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setenv("METRICS_CLOUD_FLUSH_SECONDS", "0.1")
    collector = MetricsCollector()
    collector.cloud_logger = _SlowCloudLogger()
    try:
        yield collector
    finally:
        collector._cloud_stop.set()


class TestTheCallerDoesNotWait:

    def test_recording_timings_is_fast(self, collector):
        started = time.time()
        for _ in range(50):
            collector.record_timing("agent_request", 0.1)
        elapsed = time.time() - started

        # Fifty synchronous round trips at 20 ms each would be a full second.
        assert elapsed < 0.3, f"recording blocked the caller for {elapsed:.2f}s"

    def test_the_entries_do_arrive(self, collector):
        for _ in range(10):
            collector.record_timing("agent_request", 0.1)

        assert _wait_for(lambda: len(collector.cloud_logger.entries) >= 10)

    def test_they_go_out_in_batches(self, collector):
        for _ in range(30):
            collector.record_timing("agent_request", 0.1)

        assert _wait_for(lambda: len(collector.cloud_logger.entries) >= 30)
        # Thirty entries, nothing like thirty round trips.
        assert 1 <= collector.cloud_logger.batches < 30


class TestAMetricNeverBreaksTheApp:

    def test_a_full_queue_drops_rather_than_blocks(self):
        collector = MetricsCollector()
        collector.cloud_logger = _SlowCloudLogger()
        # A queue that is already full, and nothing draining it.
        collector._cloud_queue = queue.Queue(maxsize=1)
        collector._cloud_queue.put_nowait({"filler": True})
        collector._cloud_worker = threading.current_thread()

        started = time.time()
        for _ in range(20):
            collector.record_timing("agent_request", 0.1)

        assert time.time() - started < 0.2
        assert collector._cloud_dropped == 20

    def test_a_broken_cloud_logger_is_not_fatal(self, collector):
        class _Broken:
            def log_struct(self, payload):
                raise RuntimeError("cloud is down")

            def batch(self):
                raise RuntimeError("cloud is down")

        collector.cloud_logger = _Broken()

        collector.record_timing("agent_request", 0.1)
        collector.flush_cloud(timeout=1.0)

        # The measurement itself still landed locally.
        assert collector.timing_counts["agent_request"] == 1

    def test_local_metrics_are_unaffected(self, collector):
        for _ in range(5):
            collector.record_timing("agent_request", 0.2)

        assert collector.timing_counts["agent_request"] == 5
        assert collector.timing_max["agent_request"] == pytest.approx(0.2)

    def test_nothing_is_queued_without_a_cloud_logger(self):
        collector = MetricsCollector()
        collector.cloud_logger = None

        collector.record_timing("agent_request", 0.1)

        assert collector._cloud_worker is None
        assert collector._cloud_queue.empty()


class TestFlushWaitsForTheAttempt:
    """An empty queue does not mean the sending is over.

    Not "delivered" either: the send can fail and the deadline can expire.
    What flush_cloud promises is that the attempts finished or ran out of
    time — which is the right promise for telemetry, and a much better one
    than returning while a batch is still on the wire.

    The worker pops a batch and only then makes the network call, so a flush
    that waited on the queue alone returned while a send was still on the
    wire. The worker is a daemon thread, so at shutdown the process could
    exit out from under it and the last measurements went with it.
    """

    def test_flush_waits_for_a_send_that_is_still_on_the_wire(self, monkeypatch):
        monkeypatch.setenv("METRICS_CLOUD_FLUSH_SECONDS", "0.1")
        collector = MetricsCollector()

        started = threading.Event()
        release = threading.Event()
        sent = []

        class _BlockingLogger:
            def batch(self):
                outer = self

                class _Batch:
                    def __enter__(self_inner):
                        return self_inner

                    def log_struct(self_inner, payload):
                        sent.append(payload)

                    def __exit__(self_inner, *exc):
                        # The entries are out of the queue; the network call
                        # has not finished.
                        started.set()
                        release.wait(timeout=3)
                        return False

                return _Batch()

        collector.cloud_logger = _BlockingLogger()

        try:
            collector.record_timing("agent_request", 0.1)
            assert started.wait(timeout=2), "the worker never picked it up"

            done = threading.Event()

            def flush():
                collector.flush_cloud(timeout=3.0)
                done.set()

            waiter = threading.Thread(target=flush, daemon=True)
            waiter.start()

            # The queue is already empty here. Returning now would be the bug.
            assert not done.wait(timeout=0.5), (
                "flush_cloud returned while a send was still in flight"
            )

            release.set()
            assert done.wait(timeout=3), "flush_cloud never returned"
        finally:
            release.set()
            collector._cloud_stop.set()

    def test_flush_gives_up_rather_than_hanging_a_shutdown(self, monkeypatch):
        monkeypatch.setenv("METRICS_CLOUD_FLUSH_SECONDS", "0.1")
        collector = MetricsCollector()

        release = threading.Event()

        class _StuckLogger:
            def batch(self):
                class _Batch:
                    def __enter__(self_inner):
                        return self_inner

                    def log_struct(self_inner, payload):
                        pass

                    def __exit__(self_inner, *exc):
                        release.wait(timeout=10)
                        return False

                return _Batch()

        collector.cloud_logger = _StuckLogger()

        try:
            collector.record_timing("agent_request", 0.1)
            started = time.time()
            # A cloud that is not answering must not hold up a shutdown.
            collector.flush_cloud(timeout=0.5)
            assert time.time() - started < 2.0
        finally:
            release.set()
            collector._cloud_stop.set()

    def test_the_in_flight_count_comes_back_down(self, collector):
        for _ in range(10):
            collector.record_timing("agent_request", 0.1)

        collector.flush_cloud(timeout=3.0)

        assert collector._cloud_inflight == 0
        assert len(collector.cloud_logger.entries) == 10
