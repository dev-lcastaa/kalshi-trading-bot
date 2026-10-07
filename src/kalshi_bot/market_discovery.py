"""Find currently open Kalshi 15-minute BTC/SOL markets.

Require `frequency == "fifteen_min"` and either an exact `KX<COIN>15M`
series ticker or a coin-symbol tag. Kalshi's display tags can change (e.g.
"BTC"/"SOL" became "Bitcoin"/"Solana"), so tags alone are not reliable.
Exact ticker matching avoids unrelated crypto series and head-to-head markets.
"""
from __future__ import annotations

import logging

from .kalshi_client.rest import KalshiRestClient

logger = logging.getLogger(__name__)

_FIFTEEN_MIN_FREQUENCY = "fifteen_min"


async def find_crypto_series(client: KalshiRestClient, coin_ticks: list[str]) -> list[str]:
    """Return 15-minute crypto series matching configured coin symbols."""
    data = await client.get_series_list(category="Crypto")
    series = data.get("series", [])
    coin_set = {c.upper() for c in coin_ticks}
    exact_tickers = {f"KX{coin}15M" for coin in coin_set}
    matches = []
    for s in series:
        if s.get("frequency") != _FIFTEEN_MIN_FREQUENCY:
            continue
        tags = {t.upper() for t in (s.get("tags") or [])}
        if s.get("ticker", "").upper() in exact_tickers or tags & coin_set:
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
