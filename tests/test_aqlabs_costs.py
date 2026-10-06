import pytest

for _mod in ("numpy", "scipy", "duckdb", "pyarrow"):
    pytest.importorskip(_mod)

import numpy as np  # noqa: E402

from aqlabs.costs import taker_fee, taker_fee_array  # noqa: E402
from kalshi_bot.signals.generator import kalshi_taker_fee_per_contract  # noqa: E402


def test_fee_matches_the_live_bots_fee_function_at_every_cent():
    for cents in range(0, 101):
        p = cents / 100
        assert taker_fee(p) == kalshi_taker_fee_per_contract(p)


def test_array_fee_matches_scalar_fee():
    prices = np.linspace(0, 1, 101)
    assert np.allclose(taker_fee_array(prices), [taker_fee(float(p)) for p in prices])


def test_fee_is_two_cents_near_even_odds_and_one_cent_at_extremes():
    assert taker_fee(0.5) == 0.02
    assert taker_fee(0.9) == 0.01
    assert taker_fee(0.0) == 0.0 and taker_fee(1.0) == 0.0


def test_fee_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        taker_fee(1.2)
    with pytest.raises(ValueError):
        taker_fee(0.5, multiplier=-1)
