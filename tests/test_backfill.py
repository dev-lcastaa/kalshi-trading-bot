import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest

from kalshi_bot.backtest.backfill import BackfillClient, ResearchStore, backfill, market_quality, normalize_candle, timestamp


def market(series="KXBTC15M", close="2026-09-12T00:15:00Z"):
    return {"ticker": series + "-TEST", "open_time": "2026-09-12T00:00:00Z", "close_time": close,
            "result": "yes", "strike_type": "greater_or_equal", "rules_primary": "at least",
            "floor_strike": 100, "expiration_value": "101.00",
            "custom_strike": {"round_digits": "2" if series == "KXBTC15M" else "4"}}


def candles():
    start = timestamp("2026-09-12T00:00:00Z")
    return [{"end_period_ts": start + minute * 60, "yes_bid": {"close_dollars": "0.40"},
             "yes_ask": {"close_dollars": "0.60"}, "volume_fp": "1.25"} for minute in range(1, 16)]


@pytest.fixture
def store(tmp_path):
    database = ResearchStore(str(tmp_path / "research.db"))
    yield database
    database.close()


def test_normalize_live_and_historical_dollar_schemas():
    live = candles()[0]
    archived = dict(live, yes_bid={"close": "0.40"}, yes_ask={"close": "0.60"}, volume="1.25")
    del archived["volume_fp"]
    assert normalize_candle(live)[:-1] == normalize_candle(archived)[:-1]
    assert normalize_candle(live)[1:3] == (0.4, 0.6)
    assert normalize_candle({"end_period_ts": 60})[1] is None
    with pytest.raises(ValueError):
        normalize_candle(dict(live, volume_fp="NaN"))


def test_research_store_is_idempotent_and_examples_do_not_use_future_candles(store):
    record = market()
    start, end = timestamp(record["open_time"]), timestamp(record["close_time"])
    for _ in range(2):
        store.save_market(record, "KXBTC15M", "live")
        store.save_candles(record["ticker"], candles(), start, end)
    assert store.complete(record["ticker"])
    assert store.connection.execute("SELECT COUNT(*) FROM research_candles").fetchone()[0] == 15
    rows = store.connection.execute("SELECT lead_sec,decision_ts,candle_end_ts FROM research_examples ORDER BY lead_sec").fetchall()
    assert rows == [(lead, end-lead, end-lead) for lead in (60, 180, 360)]
    store.save_candles(record["ticker"], [candles()[-1]], start, end)
    assert not store.complete(record["ticker"])
    assert store.connection.execute("SELECT COUNT(*) FROM research_examples").fetchone()[0] == 0


def test_market_quality_accepts_grouped_numbers_and_flags_bad_labels():
    record = dict(market(), floor_strike=1000, expiration_value="1,000.00")
    assert market_quality(record, "KXBTC15M") == []
    assert "settlement_result_mismatch" in market_quality(dict(record, result="no"), "KXBTC15M")
    assert "missing_or_invalid_goal_or_settlement" in market_quality(dict(record, expiration_value="1,00.00"), "KXBTC15M")


def test_unverifiable_markets_remain_raw_but_are_excluded_from_training(store):
    record = dict(market(), floor_strike=None)
    start, end = timestamp(record["open_time"]), timestamp(record["close_time"])
    store.save_market(record, "KXBTC15M", "live")
    store.save_candles(record["ticker"], candles(), start, end)
    assert store.connection.execute("SELECT COUNT(*) FROM research_examples").fetchone()[0] == 3
    assert store.connection.execute("SELECT COUNT(*) FROM research_training_examples").fetchone()[0] == 0
    assert store.summary(start, end)["quarantined_markets"] == 1


def test_refuses_live_database(tmp_path):
    path = tmp_path / "live.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE signals(ticker TEXT)")
    with pytest.raises(ValueError, match="non-research"):
        ResearchStore(str(path))


@pytest.mark.asyncio
async def test_pagination_and_close_range_filters():
    async with BackfillClient("https://example.test") as client:
        client._get = AsyncMock(side_effect=[{"markets": [market()], "cursor": "next"}, {"markets": [], "cursor": ""}])
        rows = [row async for row in client.markets("KXBTC15M", 0, 2000000000, False)]
        assert len(rows) == 1
        assert client._get.call_args_list[0].args[1]["min_close_ts"] == 0
        assert "status" not in client._get.call_args_list[0].args[1]
        assert client._get.call_args_list[1].args[1]["cursor"] == "next"
        client._get = AsyncMock(return_value={"markets": [], "cursor": "repeat"})
        with pytest.raises(RuntimeError, match="Repeated"):
            _ = [row async for row in client.markets("KXBTC15M", 0, 2000000000, True)]


@pytest.mark.asyncio
async def test_candle_404_retries_other_tier():
    request = httpx.Request("GET", "https://example.test")
    error = httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))
    async with BackfillClient("https://example.test") as client:
        client._get = AsyncMock(side_effect=[error, {"candlesticks": []}])
        await client.candles("KXBTC15M", "T", 0, 900, False)
        assert client._get.call_args_list[1].args[0] == "/historical/markets/T/candlesticks"


@pytest.mark.asyncio
async def test_backfill_resume_and_archive_routing(store):
    async with BackfillClient("https://example.test") as client:
        client.cutoff = AsyncMock(return_value={"market_settled_ts": "2026-09-13T00:00:00Z"})
        async def pages(series, start, end, historical):
            if historical:
                yield market(series)
        client.markets = pages
        client.candles = AsyncMock(return_value={"candlesticks": candles()})
        start, end = timestamp("2026-09-12T00:00:00Z"), timestamp("2026-09-13T00:00:00Z")
        result = await backfill(client, store, start, end)
        assert result["discovered"] == 2
        assert result["candles"] == 30
        assert all(call.args[-1] for call in client.candles.call_args_list)
        await backfill(client, store, start, end)
        assert client.candles.await_count == 2
        assert len(result["examples"]) == 6


@pytest.mark.asyncio
async def test_empty_candles_are_retried_on_resume(store):
    async with BackfillClient("https://example.test") as client:
        client.cutoff = AsyncMock(return_value={"market_settled_ts": "2026-07-15T00:00:00Z"})
        async def pages(series, start, end, historical):
            yield market(series)
        client.markets = pages
        client.batch_candles = AsyncMock(return_value={"markets": []})
        client.candles = AsyncMock(return_value={"candlesticks": []})
        start, end = timestamp("2026-09-12T00:00:00Z"), timestamp("2026-09-13T00:00:00Z")
        await backfill(client, store, start, end)
        client.candles.return_value = {"candlesticks": candles()}
        result = await backfill(client, store, start, end)
        assert client.candles.await_count == 4
        assert result["candles"] == 30


@pytest.mark.asyncio
async def test_batch_candles_are_used_and_trimmed_per_market(store):
    async with BackfillClient("https://example.test") as client:
        client.cutoff = AsyncMock(return_value={"market_settled_ts": "2026-07-15T00:00:00Z"})
        async def pages(series, start, end, historical):
            yield market(series)
        async def batch(tickers, start, end):
            extra = dict(candles()[-1], end_period_ts=end + 60)
            return {"markets": [{"market_ticker": ticker, "candlesticks": candles() + [extra]} for ticker in tickers]}
        client.markets = pages
        client.batch_candles = AsyncMock(side_effect=batch)
        client.candles = AsyncMock()
        start, end = timestamp("2026-09-12T00:00:00Z"), timestamp("2026-09-13T00:00:00Z")
        result = await backfill(client, store, start, end)
        assert client.candles.await_count == 0
        assert client.batch_candles.await_count == 2
        assert result["candles"] == 30