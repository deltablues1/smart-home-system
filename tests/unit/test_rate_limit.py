"""
A ceiling on how fast one caller can start paid work.

The daily budget stops spending after the money is gone. This is the part that
stops a device on the LAN reaching that ceiling inside a minute — which is what
false wake-word triggers did to the Anthropic credit on 2026-08-30, with nothing
on this side counting the requests.

Only the routes that cost money are limited. Throttling status and metrics would
lock you out of the diagnostics exactly when something is looping.

Run with:
    pytest tests/unit/test_rate_limit.py -v
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.app import RateLimitMiddleware


def _client(limit=3, window=60.0):
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limit=limit, window_seconds=window)

    @app.post("/api/chat")
    async def chat():
        return {"ok": True}

    @app.get("/api/status")
    async def status():
        return {"ok": True}

    @app.get("/api/sessions")
    async def sessions():
        return {"ok": True}

    return TestClient(app)


class TestPaidRoutes:
    def test_requests_under_the_limit_pass(self):
        client = _client(limit=3)
        assert all(client.post("/api/chat").status_code == 200 for _ in range(3))

    def test_the_one_past_the_limit_is_refused(self):
        client = _client(limit=3)
        for _ in range(3):
            client.post("/api/chat")
        response = client.post("/api/chat")
        assert response.status_code == 429

    def test_it_says_when_to_come_back(self):
        client = _client(limit=1)
        client.post("/api/chat")
        response = client.post("/api/chat")
        assert int(response.headers["Retry-After"]) > 0

    def test_it_says_this_is_protection_not_a_fault(self):
        client = _client(limit=1)
        client.post("/api/chat")
        assert "ne kvar" in client.post("/api/chat").json()["detail"]


class TestUnpaidRoutesStayOpen:
    @pytest.mark.parametrize("path", ["/api/status", "/api/sessions"])
    def test_diagnostics_are_never_throttled(self, path):
        """Locking yourself out of the metrics while something loops is the
        opposite of useful."""
        client = _client(limit=1)
        assert all(client.get(path).status_code == 200 for _ in range(10))


class TestTheWindow:
    def test_an_expired_window_lets_traffic_through_again(self):
        client = _client(limit=1, window=0.0)
        client.post("/api/chat")
        assert client.post("/api/chat").status_code == 200


class TestForwardedHeadersAreNotTrusted:
    def test_spoofing_the_client_ip_does_not_reset_the_count(self):
        """X-Forwarded-For is attacker-controlled; trusting it would make the
        limit a suggestion."""
        client = _client(limit=2)
        client.post("/api/chat", headers={"X-Forwarded-For": "1.1.1.1"})
        client.post("/api/chat", headers={"X-Forwarded-For": "2.2.2.2"})
        blocked = client.post("/api/chat", headers={"X-Forwarded-For": "3.3.3.3"})
        assert blocked.status_code == 429
