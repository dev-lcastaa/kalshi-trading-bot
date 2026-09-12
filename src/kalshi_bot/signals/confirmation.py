"""Multi-signal agreement check that gates locked-in trade decisions.

A single probability model can be confidently wrong. Before locking in a
BET UP / BET DOWN decision (see main.py's decision-lock logic), require that
independent momentum/order-book signals aren't pointing the other way - if a
majority disagree, the decision downgrades to NO_EDGE ("NO TRADE") rather
than a shaky directional call. This does not affect the continuously
recomputed live signal/recommendation shown elsewhere on the dashboard -
only the one-shot locked decision that's meant to dictate a trade.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..features.engine import Features

# Require at least this many independent signals before we'll veto the model;
# with fewer, there isn't enough independent evidence to challenge it.
MIN_SIGNALS_REQUIRED = 2
# Fraction of available signals that must agree with the model's direction.
MIN_AGREE_RATIO = 0.5
# Momentum/imbalance readings this close to zero are treated as uninformative
# noise rather than a vote either way.
_NEUTRAL_EPS = 1e-9


@dataclass(frozen=True)
class CheckVote:
    name: str
    agree: bool
    value: float


@dataclass(frozen=True)
class ConfirmationResult:
    agree: int
    total: int
    confirmed: bool  # False only when enough signals exist AND most disagree
    checks: tuple[CheckVote, ...] = ()


def check_confirmation(features: Features, call_up: bool) -> ConfirmationResult:
    """Vote independent signals (momentum at two timescales, book imbalance)
    against the model's directional call (`call_up` = model_p_yes >= 0.5)."""
    checks: list[CheckVote] = []
    for name, momentum in (
        ("OLS momentum (full window)", features.momentum_ols_per_sec),
        ("Short-term momentum", features.momentum_short_per_sec),
    ):
        if abs(momentum) > _NEUTRAL_EPS:
            checks.append(CheckVote(name=name, agree=(momentum > 0) == call_up, value=momentum))
    if features.book_imbalance is not None and abs(features.book_imbalance) > _NEUTRAL_EPS:
        checks.append(
            CheckVote(
                name="Order book imbalance",
                agree=(features.book_imbalance > 0) == call_up,
                value=features.book_imbalance,
            )
        )

    agree = sum(1 for c in checks if c.agree)
    total = len(checks)
    if total < MIN_SIGNALS_REQUIRED:
        return ConfirmationResult(agree=agree, total=total, confirmed=True, checks=tuple(checks))
    return ConfirmationResult(
        agree=agree, total=total, confirmed=(agree / total) >= MIN_AGREE_RATIO, checks=tuple(checks)
    )
