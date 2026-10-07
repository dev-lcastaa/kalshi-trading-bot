from unittest.mock import AsyncMock

import httpx
import pytest

from kalshi_bot.kalshi_client.rest import KalshiRestClient
from kalshi_bot.market_discovery import find_15min_markets, find_crypto_series


@pytest.mark.parametrize("tags", [
    (["BTC", "15 min"], ["SOL", "15 min"]),
    (["15 min", "Bitcoin", "Crypto"], ["15 min", "Crypto", "Solana"]),
    (None, []),
])
async def test_discovery_recognizes_exact_series_despite_tag_changes(tags):
    client = AsyncMock(spec=KalshiRestClient)
    client.get_series_list.return_value = {"series": [
        {"ticker": "KXBTC15M", "frequency": "fifteen_min", "tags": tags[0]},
        {"ticker": "KXSOL15M", "frequency": "fifteen_min", "tags": tags[1]},
    ]}

    assert await find_crypto_series(client, ["btc", "sol"]) == ["KXBTC15M", "KXSOL15M"]
    client.get_series_list.assert_awaited_once_with(category="Crypto")


async def test_discovery_preserves_tag_matching_and_excludes_unrelated_series():
    client = AsyncMock(spec=KalshiRestClient)
    client.get_series_list.return_value = {"series": [
        {"ticker": "LEGACY", "frequency": "fifteen_min", "tags": ["btc"]},
        {"ticker": "KXBTC15M", "frequency": "daily", "tags": ["BTC"]},
        {"ticker": "KXETH15M", "frequency": "fifteen_min", "tags": ["Ethereum"]},
        {"ticker": "KXBTCDOM", "frequency": "daily", "tags": ["Bitcoin"]},
        {"ticker": "KXBTC15MEXTRA", "frequency": "fifteen_min", "tags": ["Bitcoin"]},
        {"ticker": "KXCRYPTOCOMP15M", "frequency": "fifteen_min",
         "tags": ["Head to Heads", "15 Min Markets"]},
    ]}

    assert await find_crypto_series(client, ["BTC", "SOL"]) == ["LEGACY"]


async def test_markets_from_renamed_tags_are_open_and_paginated():
    client = AsyncMock(spec=KalshiRestClient)
    client.get_series_list.return_value = {"series": [
        {"ticker": "KXBTC15M", "frequency": "fifteen_min", "tags": ["Bitcoin"]},
        {"ticker": "KXSOL15M", "frequency": "fifteen_min", "tags": ["Solana"]},
    ]}
    markets = [{"ticker": "BTC-1"}, {"ticker": "BTC-2"}, {"ticker": "SOL-1"}]
    client.get_markets.side_effect = [
        {"markets": markets[:1], "cursor": "next"},
        {"markets": markets[1:2], "cursor": ""},
        {"markets": markets[2:]},
    ]

    assert await find_15min_markets(client, ["BTC", "SOL"]) == markets
    assert [call.kwargs for call in client.get_markets.await_args_list] == [
        {"series_ticker": "KXBTC15M", "status": "open", "cursor": None},
        {"series_ticker": "KXBTC15M", "status": "open", "cursor": "next"},
        {"series_ticker": "KXSOL15M", "status": "open", "cursor": None},
    ]


async def test_no_matching_series_logs_warning(caplog):
    client = AsyncMock(spec=KalshiRestClient)
    client.get_series_list.return_value = {"series": []}

    assert await find_15min_markets(client, ["BTC", "SOL"]) == []
    assert "No 15-minute crypto series found" in caplog.text
    client.get_markets.assert_not_awaited()


async def test_discovery_does_not_hide_api_errors():
    client = AsyncMock(spec=KalshiRestClient)
    client.get_series_list.side_effect = httpx.ConnectError("unavailable")

    with pytest.raises(httpx.ConnectError, match="unavailable"):
        await find_15min_markets(client, ["BTC", "SOL"])
