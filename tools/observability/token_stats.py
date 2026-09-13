"""
Per-agent LLM token accounting.

Records prompt/output/cached token counts per agent and per model from ADK
``after_model_callback`` hooks, so we can answer "where do the tokens go?"
before deciding anything about models or providers.

Design notes:
- Pure in-memory, process-local. No external deps. Survives only for the life
  of the running service (fine for a measurement session; reset with /tokens reset).
- The recorder is fed from a framework-level callback that fires for *every*
  model call regardless of provider, so the same numbers compare Gemini today
  against Claude/GPT later (apples-to-apples).
- Cost is derived, not measured. Token counts are exact; the PRICES table is a
  convenience overlay — Gemini numbers are approximate, edit them to match your
  actual Vertex pricing. Claude numbers are list price ($/1M tokens).
"""

from __future__ import annotations

import os
import threading
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def token_stats_enabled() -> bool:
    return os.getenv("TOKEN_STATS_ENABLED", "true").lower() in ("1", "true", "yes", "on")


# --- Pricing overlay (USD per 1,000,000 tokens: input, output) -------------
# Gemini values are APPROXIMATE — confirm against your Vertex pricing and edit.
# Claude values are public list price. Used only for the cost estimate column.
PRICES: Dict[str, tuple] = {
    # model substring -> (input_per_1M, output_per_1M)
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.1-pro": (1.25, 10.00),
    "gemini-3-pro": (1.25, 10.00),
    # Claude (public list price). ORDER MATTERS: _price_for takes the first
    # substring match in insertion order, so version-specific keys come first.
    # Sonnet 5 is $2/$10; the generic "claude-sonnet" below is Sonnet 4.6's
    # $3/$15, which is what every report used until 2026-09-03 — overstating
    # the bill by ~50% for a fleet that runs on Sonnet 5.
    "claude-sonnet-5": (2.00, 10.00),
    "claude-fable": (10.00, 50.00),
    "claude-opus": (5.00, 25.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (1.00, 5.00),
}


def _price_for(model: str) -> Optional[tuple]:
    if not model:
        return None
    for key, price in PRICES.items():
        if key in model:
            return price
    return None


def cost_of(model: str, prompt: int, output: int, cached: int = 0) -> Optional[float]:
    """USD for one call, using the same arithmetic as the per-agent report.

    Shared so the daily budget ledger and /tokens can never drift apart: a
    ceiling computed differently from the report it is compared against is
    worse than no ceiling.
    """
    price = _price_for(model)
    if price is None:
        return None
    rate = float(os.getenv("CACHE_READ_RATE", "0.1"))
    uncached = max(prompt - cached, 0)
    return (uncached * price[0] + cached * price[0] * rate + output * price[1]) / 1_000_000.0


@dataclass
class CallRecord:
    agent: str
    model: str
    prompt_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int


@dataclass
class _Agg:
    calls: int = 0
    prompt: int = 0
    output: int = 0
    cached: int = 0
    total: int = 0
    model: str = ""

    def add(self, r: CallRecord) -> None:
        self.calls += 1
        self.prompt += r.prompt_tokens
        self.output += r.output_tokens
        self.cached += r.cached_tokens
        self.total += r.total_tokens
        if r.model:
            self.model = r.model

    # Cache-read input tokens bill at ~0.1x of base input price (Anthropic).
    CACHE_READ_RATE = float(os.getenv("CACHE_READ_RATE", "0.1"))

    def cost(self) -> Optional[float]:
        price = _price_for(self.model)
        if price is None:
            return None
        # 'cached' = cache-read tokens (billed ~0.1x); the rest at full input price.
        # First-call cache-write premium (~1.25x) is not separately tracked, so
        # this is a close approximation, not exact billing.
        uncached = max(self.prompt - self.cached, 0)
        input_cost = uncached * price[0] + self.cached * price[0] * self.CACHE_READ_RATE
        return (input_cost + self.output * price[1]) / 1_000_000.0


class TokenStats:
    """Thread-safe in-memory token recorder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: List[CallRecord] = []

    def record(
        self,
        agent: str,
        model: str,
        prompt_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        if not total_tokens:
            total_tokens = prompt_tokens + output_tokens
        rec = CallRecord(
            agent=agent or "unknown",
            model=model or "",
            prompt_tokens=int(prompt_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cached_tokens=int(cached_tokens or 0),
            total_tokens=int(total_tokens or 0),
        )
        with self._lock:
            self._records.append(rec)

    def mark(self) -> int:
        """Return the current record count — pass to ``report`` for a per-turn slice."""
        with self._lock:
            return len(self._records)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()

    def _aggregate(self, start: int = 0) -> Dict[str, _Agg]:
        out: Dict[str, _Agg] = {}
        with self._lock:
            slice_ = self._records[start:]
        for r in slice_:
            out.setdefault(r.agent, _Agg()).add(r)
        return out

    def report(self, start: int = 0, title: str = "TOKEN USAGE") -> str:
        agg = self._aggregate(start)
        if not agg:
            return f"{title}: (no model calls recorded)"

        rows = sorted(agg.items(), key=lambda kv: kv[1].total, reverse=True)
        lines = [f"{title}"]
        lines.append(
            f"{'agent':<18}{'model':<22}{'calls':>6}{'in':>9}{'out':>8}{'cached':>8}{'~$':>9}"
        )
        tot_calls = tot_in = tot_out = tot_cached = 0
        tot_cost = 0.0
        any_cost = False
        for agent, a in rows:
            cost = a.cost()
            cost_s = f"{cost:.4f}" if cost is not None else "n/a"
            if cost is not None:
                tot_cost += cost
                any_cost = True
            lines.append(
                f"{agent[:17]:<18}{a.model[:21]:<22}{a.calls:>6}{a.prompt:>9}"
                f"{a.output:>8}{a.cached:>8}{cost_s:>9}"
            )
            tot_calls += a.calls
            tot_in += a.prompt
            tot_out += a.output
            tot_cached += a.cached
        cost_s = f"{tot_cost:.4f}" if any_cost else "n/a"
        lines.append(
            f"{'TOTAL':<18}{'':<22}{tot_calls:>6}{tot_in:>9}{tot_out:>8}{tot_cached:>8}{cost_s:>9}"
        )
        return "\n".join(lines)


_stats: Optional[TokenStats] = None


def get_token_stats() -> TokenStats:
    global _stats
    if _stats is None:
        _stats = TokenStats()
    return _stats
