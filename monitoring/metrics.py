"""
Metrics Collection

Collects and reports metrics for monitoring and alerting
Integrates with Google Cloud Logging for dashboard visualization
"""

import atexit
import queue
import threading
import time
import logging
import json
import os
from typing import Dict, Any, Optional
from functools import wraps
from collections import defaultdict
from collections import deque
from datetime import datetime, timezone

# Import Google Cloud Logging
try:
    import google.cloud.logging
    from google.cloud.logging.handlers import CloudLoggingHandler
    GCP_LOGGING_AVAILABLE = True
except ImportError:
    GCP_LOGGING_AVAILABLE = False

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Collects application metrics and exports to Cloud Logging"""

    def __init__(self):
        self.metrics = defaultdict(int)
        # Recent samples only. An unbounded list here grows for as long as the
        # process lives, and this one lives on a Pi for weeks — every timed call
        # ever made was being kept to compute an average.
        #
        # The window is for the average; count, min and max are tracked
        # separately so they keep meaning what they have always meant. Swapping
        # in a bounded buffer alone would have quietly turned "count" into
        # "count since the buffer last filled".
        self.timing_window = max(20, int(os.getenv("METRICS_TIMING_WINDOW", "200")))
        self.timings = defaultdict(lambda: deque(maxlen=self.timing_window))
        self.timing_counts = defaultdict(int)
        self.timing_min = {}
        self.timing_max = {}
        self.errors = defaultdict(int)
        self.structured_events = deque(
            maxlen=max(50, int(os.getenv("MONITORING_EVENT_BUFFER_SIZE", "400")))
        )
        self.cloud_logger = None

        # Cloud Logging goes through a queue and a worker thread. It used to
        # be one synchronous network call per event, on the caller's thread —
        # so `record_timing` paid a round trip for every measurement it took.
        # Measured on 2026-09-05: 500 timings took 8 min 38 s with
        # USE_CLOUD_LOGGING on and 1.8 s with it off.
        #
        # That cost landed at the worst possible moment: cloud logging gets
        # switched on precisely when someone is investigating something, so
        # the instrument slowed down the thing under investigation.
        self._cloud_queue: "queue.Queue" = queue.Queue(
            maxsize=max(100, int(os.getenv("METRICS_CLOUD_QUEUE_SIZE", "2000")))
        )
        self._cloud_batch_size = max(1, int(os.getenv("METRICS_CLOUD_BATCH_SIZE", "50")))
        self._cloud_flush_seconds = max(
            0.1, float(os.getenv("METRICS_CLOUD_FLUSH_SECONDS", "5"))
        )
        self._cloud_worker = None
        self._cloud_stop = threading.Event()
        self._cloud_dropped = 0
        # Entries taken off the queue but not yet sent. An empty queue is not
        # delivery: the worker pops a batch and only then makes the network
        # call, so waiting for the queue alone returned while a send was still
        # in flight — and the worker is a daemon thread, so the process could
        # exit out from under it.
        self._cloud_inflight = 0
        self._cloud_idle = threading.Condition()

        # Initialize Cloud Logging if available and configured
        use_cloud_logging = os.getenv("USE_CLOUD_LOGGING", "").lower() == "true"
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        if GCP_LOGGING_AVAILABLE and (use_cloud_logging or project_id):
            try:
                client = google.cloud.logging.Client(project=project_id or None)
                self.cloud_logger = client.logger("adk-agent-system")
                logger.info("Cloud Logging initialized for metrics")
            except Exception as e:
                logger.warning(f"Failed to initialize Cloud Logging: {e}")

    def _log_to_cloud(self, event_type: str, payload: Dict[str, Any]):
        """Hand a structured entry to the Cloud Logging worker.

        Never blocks and never raises: a metric is not worth slowing the thing
        it measures, and it is certainly not worth failing it. A full queue
        drops entries and counts them rather than making anyone wait.
        """
        if not self.cloud_logger:
            return
        try:
            payload["event_type"] = event_type
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
            self._ensure_cloud_worker()
            self._cloud_queue.put_nowait(payload)
        except queue.Full:
            self._cloud_dropped += 1
            if self._cloud_dropped % 100 == 1:
                logger.warning(
                    "Cloud Logging queue full — dropped %d metric event(s)",
                    self._cloud_dropped,
                )
        except Exception as e:
            logger.debug(f"Failed to queue log for Cloud: {e}")

    def _ensure_cloud_worker(self) -> None:
        if self._cloud_worker is not None and self._cloud_worker.is_alive():
            return
        self._cloud_stop.clear()
        self._cloud_worker = threading.Thread(
            target=self._cloud_loop, name="metrics-cloud-logging", daemon=True
        )
        self._cloud_worker.start()
        atexit.register(self.flush_cloud)

    def _cloud_loop(self) -> None:
        """Drain the queue in batches until asked to stop."""
        while not self._cloud_stop.is_set():
            self._handle(self._drain(block=True))
        # Whatever is left when stopping.
        self._handle(self._drain(block=False))

    def _handle(self, batch: list) -> None:
        """Send a batch, keeping the in-flight count honest either way."""
        if not batch:
            return
        try:
            self._send_batch(batch)
        finally:
            with self._cloud_idle:
                self._cloud_inflight -= len(batch)
                self._cloud_idle.notify_all()

    def _drain(self, *, block: bool) -> list:
        batch = []
        try:
            if block:
                batch.append(self._cloud_queue.get(timeout=self._cloud_flush_seconds))
        except queue.Empty:
            return batch
        while len(batch) < self._cloud_batch_size:
            try:
                batch.append(self._cloud_queue.get_nowait())
            except queue.Empty:
                break
        # Counted as in flight the moment they leave the queue, so a flush can
        # see them.
        with self._cloud_idle:
            self._cloud_inflight += len(batch)
        return batch

    def _send_batch(self, batch: list) -> None:
        """One API call for the whole batch where the client supports it."""
        try:
            make_batch = getattr(self.cloud_logger, "batch", None)
            if callable(make_batch):
                with make_batch() as cloud_batch:
                    for entry in batch:
                        cloud_batch.log_struct(entry)
            else:
                for entry in batch:
                    self.cloud_logger.log_struct(entry)
        except Exception as e:
            logger.debug(f"Failed to send log batch to Cloud: {e}")

    def flush_cloud(self, timeout: float = 5.0) -> None:
        """Wait for the queued send attempts to finish, up to `timeout`.

        Waits on both halves: the queue, and the batch the worker has already
        taken out of it. Checking the queue alone returned while a send was
        still on the wire, which at shutdown meant the last measurements went
        with the process — the worker is a daemon thread and nothing was
        joining it.

        Deliberately not a delivery guarantee. `_send_batch` swallows its
        errors and the deadline allows giving up, so what returning means is
        "the attempts finished or ran out of time", not "the entries arrived".
        That is the right contract for telemetry: a cloud that is not
        answering must not hold up a shutdown, and a metric must not be the
        reason anything fails.
        """
        worker = self._cloud_worker
        if worker is None:
            return

        deadline = time.time() + timeout
        with self._cloud_idle:
            while time.time() < deadline:
                if self._cloud_queue.empty() and self._cloud_inflight <= 0:
                    break
                self._cloud_idle.wait(timeout=0.05)

        self._cloud_stop.set()
        remaining = deadline - time.time()
        if remaining > 0 and worker.is_alive():
            worker.join(timeout=remaining)
        if worker.is_alive():
            logger.warning(
                "Cloud Logging worker did not finish within %.1fs — "
                "%d queued, %d in flight",
                timeout, self._cloud_queue.qsize(), self._cloud_inflight,
            )

    def log_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        store_local: bool = True,
    ):
        """Store and export a structured event."""
        event = dict(payload)
        event["event_type"] = event_type
        event["timestamp"] = datetime.now(timezone.utc).isoformat()

        if store_local:
            self.structured_events.append(event)

        self._log_to_cloud(event_type, dict(payload))

    def get_recent_events(
        self,
        event_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        """Return recent structured events, newest first."""
        items = list(self.structured_events)
        if event_type:
            items = [item for item in items if item.get("event_type") == event_type]
        if limit > 0:
            items = items[-limit:]
        items.reverse()
        return items

    def increment(self, metric_name: str, value: int = 1, labels: Optional[Dict] = None):
        """Increment a counter metric"""
        key = self._make_key(metric_name, labels)
        self.metrics[key] += value
        logger.debug(f"Metric incremented: {key} += {value}")
        
        # Export to Cloud Logging
        payload = {
            "metric_name": metric_name,
            "value": value,
            "metric_type": "counter"
        }
        if labels:
            payload.update(labels)
            
        self._log_to_cloud(f"{metric_name}_event", payload)

    def record_timing(self, metric_name: str, duration: float, labels: Optional[Dict] = None):
        """Record a timing metric"""
        key = self._make_key(metric_name, labels)
        self.timings[key].append(duration)
        self.timing_counts[key] += 1
        if key not in self.timing_min or duration < self.timing_min[key]:
            self.timing_min[key] = duration
        if key not in self.timing_max or duration > self.timing_max[key]:
            self.timing_max[key] = duration
        logger.debug(f"Timing recorded: {key} = {duration:.3f}s")
        
        # Export to Cloud Logging
        payload = {
            "metric_name": metric_name,
            "duration_ms": duration * 1000,  # Convert to ms
            "metric_type": "timing"
        }
        if labels:
            payload.update(labels)
            
        self._log_to_cloud(f"{metric_name}_event", payload)

    def record_error(self, error_type: str, labels: Optional[Dict] = None):
        """Record an error"""
        key = self._make_key(error_type, labels)
        self.errors[key] += 1
        logger.debug(f"Error recorded: {key}")
        
        # Export to Cloud Logging
        payload = {
            "error_type": error_type,
            "metric_type": "error"
        }
        if labels:
            payload.update(labels)
            
        self._log_to_cloud("agent_request_error", payload)

    def get_metrics(self) -> Dict[str, Any]:
        """Get all collected metrics"""
        return {
            "counters": dict(self.metrics),
            "timings": {
                k: {
                    # Count, min and max are lifetime figures; the average is
                    # over the retained window, and says how many that was.
                    "count": self.timing_counts.get(k, len(v)),
                    "avg": sum(v) / len(v) if v else 0,
                    "avg_over_last": len(v),
                    "min": self.timing_min.get(k, min(v) if v else 0),
                    "max": self.timing_max.get(k, max(v) if v else 0),
                }
                for k, v in self.timings.items()
            },
            "errors": dict(self.errors),
            "recent_event_count": len(self.structured_events),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    def get_summary(self) -> Dict[str, Any]:
        """Get metrics summary (alias for get_metrics for backwards compatibility)"""
        return self.get_metrics()

    def reset(self):
        """Reset all metrics"""
        self.metrics.clear()
        self.timings.clear()
        self.timing_counts.clear()
        self.timing_min.clear()
        self.timing_max.clear()
        self.errors.clear()
        self.structured_events.clear()

    def _make_key(self, name: str, labels: Optional[Dict] = None) -> str:
        """Create metric key with labels"""
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}[{label_str}]"


# Global metrics collector
_metrics_collector = MetricsCollector()


def get_metrics_collector() -> MetricsCollector:
    """Get global metrics collector"""
    return _metrics_collector


def track_time(metric_name: str):
    """Decorator to track execution time"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                duration = time.time() - start_time
                _metrics_collector.record_timing(
                    metric_name,
                    duration,
                    labels={"function": func.__name__}
                )
        return wrapper
    return decorator


def track_errors(error_type: str = "error"):
    """Decorator to track errors"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                _metrics_collector.record_error(
                    error_type,
                    labels={
                        "function": func.__name__,
                        "exception": type(e).__name__
                    }
                )
                raise
        return wrapper
    return decorator


# Example usage in agents
class AgentMetrics:
    """Metrics specific to agents"""

    @staticmethod
    def record_agent_call(agent_name: str, success: bool = True):
        """Record agent invocation"""
        event_type = "agent_request_success" if success else "agent_request_error"
        
        # For success, we use increment but specify the event type in _log_to_cloud
        # However, increment uses {metric_name}_event. 
        # So we need to be careful to match the dashboard filters:
        # jsonPayload.event_type="agent_request_success"
        
        if success:
            _metrics_collector.cloud_logger.log_struct({
                "event_type": "agent_request_success",
                "agent_name": agent_name,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }) if _metrics_collector.cloud_logger else None
            
            _metrics_collector.increment(
                "agent_calls",
                labels={"agent": agent_name, "success": "True"}
            )
        else:
            _metrics_collector.cloud_logger.log_struct({
                "event_type": "agent_request_error",
                "agent_name": agent_name,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }) if _metrics_collector.cloud_logger else None
            
            _metrics_collector.increment(
                "agent_calls",
                labels={"agent": agent_name, "success": "False"}
            )

    @staticmethod
    def record_tool_call(agent_name: str, tool_name: str, success: bool = True):
        """Record tool invocation"""
        if success:
            _metrics_collector.cloud_logger.log_struct({
                "event_type": "tool_execution_success",
                "agent_name": agent_name,
                "tool_name": tool_name,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }) if _metrics_collector.cloud_logger else None

        _metrics_collector.increment(
            "tool_calls",
            labels={
                "agent": agent_name,
                "tool": tool_name,
                "success": str(success)
            }
        )

    @staticmethod
    def record_routing(from_agent: str, to_agent: str):
        """Record agent routing"""
        _metrics_collector.increment(
            "agent_routing",
            labels={"from": from_agent, "to": to_agent}
        )
