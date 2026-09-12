from kalshi_bot.features.engine import Features
from kalshi_bot.signals.confirmation import check_confirmation


def _features(momentum_ols=0.0, momentum_short=0.0, book_imbalance=None):
    return Features(
        index_price=100.0,
        strike=100.0,
        seconds_to_expiry=300.0,
        realized_vol_per_sqrt_sec=0.001,
        momentum_per_sec=0.0,
        book_imbalance=book_imbalance,
        momentum_ols_per_sec=momentum_ols,
        momentum_short_per_sec=momentum_short,
    )


def test_confirmed_when_all_signals_agree_with_up_call():
    features = _features(momentum_ols=0.001, momentum_short=0.002, book_imbalance=0.5)
    result = check_confirmation(features, call_up=True)
    assert result.confirmed is True
    assert result.agree == result.total == 3


def test_vetoed_when_majority_disagree_with_up_call():
    features = _features(momentum_ols=-0.001, momentum_short=-0.002, book_imbalance=0.5)
    result = check_confirmation(features, call_up=True)
    assert result.confirmed is False
    assert result.agree == 1
    assert result.total == 3


def test_confirmed_when_fewer_than_min_signals_available():
    # Only momentum_ols is non-neutral; momentum_short is ~0 and book_imbalance is None.
    features = _features(momentum_ols=-0.001, momentum_short=0.0, book_imbalance=None)
    result = check_confirmation(features, call_up=True)
    assert result.total == 1
    assert result.confirmed is True  # not enough independent evidence to veto


def test_neutral_signals_excluded_from_vote():
    features = _features(momentum_ols=0.0, momentum_short=0.0, book_imbalance=0.0)
    result = check_confirmation(features, call_up=True)
    assert result.total == 0
    assert result.confirmed is True


def test_down_call_agreement_uses_negative_signals():
    features = _features(momentum_ols=-0.001, momentum_short=-0.002, book_imbalance=-0.5)
    result = check_confirmation(features, call_up=False)
    assert result.confirmed is True
    assert result.agree == result.total == 3


def test_tie_counts_as_confirmed_at_min_agree_ratio():
    # 1 of 2 agree -> ratio exactly 0.5, meets MIN_AGREE_RATIO.
    features = _features(momentum_ols=0.001, momentum_short=-0.001, book_imbalance=None)
    result = check_confirmation(features, call_up=True)
    assert result.total == 2
    assert result.agree == 1
    assert result.confirmed is True
