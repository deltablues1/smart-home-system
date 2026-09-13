"""
Process liveness guards for the long-running systemd services.

Two failure modes seen in production (2026-08-18 Telegram incident) are covered
here:

1. **The process refuses to die.** ``sys.exit()`` only raises ``SystemExit``;
   the interpreter still waits for every non-daemon thread. The Google Cloud
   Logging handler keeps such a thread alive and can block forever flushing its
   backlog ("Failed to send N pending logs"), so a service that logged a fatal
   error stayed alive for two days with ``Restart=always`` never firing.
   ``hard_exit()`` flushes what it can with a bounded timeout and then calls
   ``os._exit()``, which no thread can veto.

2. **The process is alive but no longer working.** systemd only watches the PID,
   so a dead poller inside a live process reads as ``active (running)``.
   ``sd_notify()`` lets the service prove it is still doing its job to
   ``WatchdogSec=``; if the heartbeat stops, systemd kills and restarts it.
"""

import logging
import os
import socket
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Flushing Cloud Logging can block on the network; never wait longer than this.
_FLUSH_TIMEOUT_SECONDS = float(os.getenv("SHUTDOWN_FLUSH_TIMEOUT_SECONDS", "5"))


def flush_logging(timeout: float = _FLUSH_TIMEOUT_SECONDS) -> bool:
    """Flush and close every logging handler, but never block past ``timeout``.

    Returns True if the flush completed, False if it timed out (in which case
    log records may be lost — that is deliberate: losing logs beats hanging).
    """
    done = threading.Event()

    def _flush():
        for handler in list(logging.getLogger().handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:  # pragma: no cover - best effort by design
                pass
        done.set()

    worker = threading.Thread(target=_flush, name="log-flush", daemon=True)
    worker.start()
    return done.wait(timeout)


def hard_exit(code: int = 1, reason: str = "") -> None:
    """Terminate the process immediately, whatever threads are still running.

    Use instead of ``sys.exit()`` in the fatal-error path of a systemd service:
    only a real process exit makes ``Restart=always`` kick in.
    """
    if reason:
        try:
            logger.critical("Hard exit (%s): %s", code, reason)
        except Exception:  # pragma: no cover
            pass
    # stderr rather than logging: the handlers are about to be torn down.
    print(f"[process_guard] exiting with code {code}: {reason}", file=sys.stderr, flush=True)
    flush_logging()
    os._exit(code)


def sd_notify(state: str) -> bool:
    """Send a datagram to the systemd notify socket (no-op when not under systemd).

    Pure socket implementation so the services keep no extra dependency.
    """
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return False
    try:
        # A leading '@' means an abstract namespace socket.
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
        return True
    except Exception as exc:  # pragma: no cover - depends on the systemd runtime
        logger.debug("sd_notify(%s) failed: %s", state, exc)
        return False


def notify_ready(status: Optional[str] = None) -> bool:
    """Tell systemd the service finished starting up."""
    payload = "READY=1"
    if status:
        payload += f"\nSTATUS={status}"
    return sd_notify(payload)


def notify_watchdog(status: Optional[str] = None) -> bool:
    """Send one watchdog heartbeat. Call only while the service is truly healthy."""
    payload = "WATCHDOG=1"
    if status:
        payload += f"\nSTATUS={status}"
    return sd_notify(payload)


def watchdog_interval_seconds() -> Optional[float]:
    """Half of ``WatchdogSec=`` (the interval systemd expects), or None if unset."""
    raw = os.getenv("WATCHDOG_USEC")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    return (usec / 1_000_000) / 2
