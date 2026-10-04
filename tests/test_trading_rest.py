from unittest.mock import Mock

import httpx
import pytest

from kalshi_bot.kalshi_client.rest import KalshiRestClient


@pytest.mark.asyncio
async def test_event_order_is_signed_once_and_not_retried():
    auth = Mock()
    auth.headers.return_value = {"KALSHI-ACCESS-KEY": "test"}
    client = KalshiRestClient("https://example.test/trade-api/v2", auth)
    await client._client.aclose()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={"message": "unavailable"})

    client._client = httpx.AsyncClient(
        base_url="https://example.test/trade-api/v2", transport=httpx.MockTransport(respond),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.create_event_order({"ticker": "BTC", "side": "bid", "count": "1", "price": "0.45"})
    assert len(requests) == 1
    assert requests[0].url.path == "/trade-api/v2/portfolio/events/orders"
    auth.headers.assert_called_once_with("POST", "/trade-api/v2/portfolio/events/orders")
    await client.aclose()