"""
Cost reports have to name the model that actually ran.

_price_for takes the first substring match in insertion order. "claude-sonnet"
sat above everything and carried Sonnet 4.6's $3/$15, so every report for a
fleet running claude-sonnet-5 ($2/$10) overstated the bill by ~50%. Fable had
no entry at all, which prices it at nothing rather than at $10/$50.

Run with:
    pytest tests/unit/test_token_prices.py -v
"""

import pytest

from tools.observability.token_stats import PRICES, _Agg, _price_for


class TestCurrentModelsArePricedCorrectly:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("claude-sonnet-5", (2.00, 10.00)),
            ("claude-opus-5", (5.00, 25.00)),
            ("claude-fable-5-1", (10.00, 50.00)),
            ("claude-haiku-4-5", (1.00, 5.00)),
        ],
    )
    def test_price(self, model, expected):
        assert _price_for(model) == expected

    def test_litellm_prefixed_names_still_match(self):
        assert _price_for("anthropic/claude-sonnet-5") == (2.00, 10.00)

    def test_the_older_generic_key_still_serves_sonnet_4_6(self):
        assert _price_for("claude-sonnet-4-6") == (3.00, 15.00)

    def test_specific_keys_precede_generic_ones(self):
        keys = list(PRICES)
        assert keys.index("claude-sonnet-5") < keys.index("claude-sonnet")


class TestCacheReadsStayDiscounted:
    def test_cached_input_is_billed_at_a_tenth(self):
        agg = _Agg()
        agg.model = "claude-sonnet-5"
        agg.prompt, agg.cached, agg.output = 10_000, 8_000, 0

        # 2000 full + 8000 at 0.1x = 2800 token-equivalents at $2/1M
        assert agg.cost() == pytest.approx(2_800 * 2.00 / 1_000_000)
