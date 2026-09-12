"""Find currently open Kalshi 15-minute BTC/SOL markets.

Kalshi tags its true 15-minute crypto series explicitly (verified against the
live API): `frequency == "fifteen_min"` and a `tags` entry matching the coin
(e.g. `KXBTC15M` has frequency "fifteen_min" and tags ["BTC", "15 min"]).
Matching on that instead of ticker substrings avoids false positives like
`KXBTCDOM`, `KXBTCMAXY`, `KXSOLNASDAQ`, etc. (there are 277 series under
category=Crypto; naive substring matching on "BTC"/"SOL" catches ~83 of them).
"""
from __future__ import annotations

import logging

from .kalshi_client.rest import KalshiRestClient

logger = logging.getLogger(__name__)

_FIFTEEN_MIN_FREQUENCY = "fifteen_min"


async def find_crypto_series(client: KalshiRestClient, coin_ticks: list[str]) -> list[str]:
    """Return 15-minute crypto series tickers tagged with one of coin_ticks."""
    data = await client.get_series_list(category="Crypto")
    series = data.get("series", [])
    coin_set = {c.upper() for c in coin_ticks}
    matches = []
    for s in series:
        if s.get("frequency") != _FIFTEEN_MIN_FREQUENCY:
            continue
        tags = {t.upper() for t in (s.get("tags") or [])}
        if tags & coin_set:
            matches.append(s["ticker"])
    return matches


async def find_15min_markets(
    client: KalshiRestClient, coin_ticks: list[str]
) -> list[dict]:
    """Return open markets across the matching 15-minute crypto series."""
    series_tickers = await find_crypto_series(client, coin_ticks)
    if not series_tickers:
        logger.warning("No 15-minute crypto series found matching %s", coin_ticks)

    results: list[dict] = []
    for series_ticker in series_tickers:
        cursor = None
        while True:
            page = await client.get_markets(
                series_ticker=series_ticker, status="open", cursor=cursor
            )
            results.extend(page.get("markets", []))
            cursor = page.get("cursor")
            if not cursor:
                break
    return results
