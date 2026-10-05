import math
from decimal import Decimal

import pytest

from kalshi_bot.prediction.fair_value import coin_z_score, fair_value_from_ticks, fair_value_p_yes
from kalshi_bot.trading import EntryRule, default_rules, parse_rules, rules_json

NOW = 10_000_000_000


def ticks(final: float, minutes: int = 31, start: float = 100.0):
    """One tick per second, alternating +-0.1% per minute, ending at `final`."""
    rows = []
    for second in range(minutes * 60, -1, -1):
        minute = second // 60
        rows.append((NOW - second * 1000, start * (1.001 if minute % 2 else 0.999)))
    rows[-1] = (NOW, final)
    return rows


def test_z_score_measures_distance_in_remaining_moves():
    z, minutes_left = coin_z_score(ticks(100.5), 100.0, NOW + 5 * 60_000, NOW)
    assert minutes_left == pytest.approx(5)
    assert z > 1


def test_z_score_needs_fresh_history_and_time_left():
    assert coin_z_score(ticks(100.5, minutes=5), 100.0, NOW + 5 * 60_000, NOW) is None
    assert coin_z_score(ticks(100.5), 100.0, NOW + 30_000, NOW) is None
    assert coin_z_score(ticks(100.5), 100.0, NOW + 5 * 60_000, NOW + 10_000) is None


def test_z_score_rejects_invalid_ticks_and_stale_minute_endpoints():
    rows = ticks(100.5)
    for invalid in (list(reversed(rows)), rows + [rows[-1]], rows[:-1] + [(NOW, float("nan"))]):
        assert coin_z_score(invalid, 100, NOW + 300000, NOW) is None
    # Only fresh starts, with no fresh endpoints: these are not valid minute returns.
    sparse = [(NOW - minute * 60000, 100 + minute * .01) for minute in range(31, 0, -2)]
    sparse.append((NOW, 100.5))
    assert coin_z_score(sparse, 100, NOW + 300000, NOW) is None


@pytest.mark.parametrize("p,z,t", [(float("nan"), 0, 5), (.5, float("inf"), 5), (1.1, 0, 5), (.5, 0, .5)])
def test_fair_value_invalid_inputs_surface_explicitly(p, z, t):
    with pytest.raises(ValueError):
        fair_value_p_yes(p, z, t, True)


def test_coin_above_strike_raises_fair_value_above_market():
    above = fair_value_from_ticks(ticks(100.5), 100.0, NOW + 10 * 60_000, NOW, 0.5, "BRTI")
    below = fair_value_from_ticks(ticks(99.5), 100.0, NOW + 10 * 60_000, NOW, 0.5, "BRTI")
    assert above > 0.55 and below < 0.45


def test_coin_weight_grows_with_time_left():
    early = fair_value_p_yes(0.5, 1.0, 14, True)
    late = fair_value_p_yes(0.5, 1.0, 2, True)
    assert early > late > 0.5
    assert fair_value_p_yes(0.5, 0.0, 10, False) == pytest.approx(0.5, abs=0.01)
    assert not math.isnan(fair_value_p_yes(0.99, 8.0, 10, False))


def test_model_side_buys_the_larger_after_fee_edge():
    rule = EntryRule()
    # The model leans YES (0.55) but YES is expensive and NO is cheap.
    assert rule.pick_side(Decimal("0.55"), Decimal("0.60"), Decimal("0.38")) == "no"
    assert rule.pick_side(Decimal("0.55"), Decimal("0.50"), Decimal("0.52")) == "yes"
    assert rule.pick_side(Decimal("0.55")) == "yes"


def test_scale_in_fields_round_trip_and_validate():
    base = {"name": "A", "budget": "1"}
    rule = parse_rules({"rules": [dict(base, max_entries=5, reentry_gap_sec=30)]})[0]
    assert (rule.max_entries, rule.reentry_gap_sec) == (5, 30)
    assert parse_rules(rules_json([rule]))[0] == rule
    legacy = parse_rules({"rules": [base]})[0]
    assert (legacy.max_entries, legacy.reentry_gap_sec) == (1, 60)
    for bad in ({"max_entries": 0}, {"max_entries": 11}, {"reentry_gap_sec": 901}, {"max_entries": 1.5}):
        with pytest.raises(ValueError):
            parse_rules({"rules": [dict(base, **bad)]})


def test_default_rule_is_the_backtested_coin_price_edge():
    rule = default_rules()[0]
    assert rule.side == "model" and rule.min_edge == Decimal("0.03")
    assert (rule.min_seconds_left, rule.max_seconds_left) == (240, 840)
    assert (rule.max_entries, rule.reentry_gap_sec) == (5, 60)
    assert not rule.policy.has_exits
