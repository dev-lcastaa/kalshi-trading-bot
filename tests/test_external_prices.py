from kalshi_bot.external_prices import aggregate_external_prices


def test_aggregate_external_prices_filters_stale_rows_and_computes_median():
    rows = [
        {"source": "coinbase", "index_id": "BRTI", "price": 100.0, "received_at_ms": 9_000},
        {"source": "kraken", "index_id": "BRTI", "price": 102.0, "received_at_ms": 9_500},
        {"source": "old", "index_id": "BRTI", "price": 1.0, "received_at_ms": 1_000},
        {"source": "coinbase", "index_id": "SOLUSD_RTI", "price": 50.0, "received_at_ms": 9_000},
    ]

    result = aggregate_external_prices(rows, now_ms=10_000, max_age_ms=1_000)

    assert result["BRTI"]["price"] == 101.0
    assert result["BRTI"]["source_count"] == 2
    assert result["BRTI"]["max_age_ms"] == 1_000
    assert result["BRTI"]["sources"] == ["coinbase", "kraken"]
    assert result["SOLUSD_RTI"]["price"] == 50.0


def test_aggregate_external_prices_has_no_stale_result():
    assert aggregate_external_prices(
        [{"source": "coinbase", "index_id": "BRTI", "price": 100.0, "received_at_ms": 1}],
        now_ms=10_000,
        max_age_ms=5_000,
    ) == {}
