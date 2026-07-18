"""Tests for derivatives collector Binance → Bybit/OKX fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ainvestor.collectors.derivatives import DerivativesCollector


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setattr(
        "ainvestor.collectors.derivatives.get_all_market_pairs",
        lambda: ["ETH/USDT"],
    )
    return DerivativesCollector()


def _response(status: int, *, json_data=None, text: str = "", url: str = "https://test") -> httpx.Response:
    kwargs: dict = {"request": httpx.Request("GET", url)}
    if json_data is not None:
        kwargs["json"] = json_data
    else:
        kwargs["text"] = text
    return httpx.Response(status, **kwargs)


@pytest.mark.asyncio
async def test_binance_451_falls_back_to_okx(collector):
    async def fake_get(url, params=None):
        url = str(url)
        if "fapi.binance.com" in url:
            return _response(451, text="Unavailable For Legal Reasons", url=url)
        if "okx.com/api/v5/public/funding-rate" in url:
            return _response(
                200,
                url=url,
                json_data={
                    "code": "0",
                    "data": [
                        {
                            "instId": "ETH-USDT-SWAP",
                            "fundingRate": "0.00012",
                            "nextFundingTime": "1784073600000",
                        }
                    ],
                },
            )
        if "open-interest" in url:
            return _response(
                200,
                url=url,
                json_data={"code": "0", "data": [{"instId": "ETH-USDT-SWAP", "oi": "123456.78"}]},
            )
        if "mark-price" in url:
            return _response(
                200,
                url=url,
                json_data={
                    "code": "0",
                    "data": [{"instId": "ETH-USDT-SWAP", "markPx": "3500.5"}],
                },
            )
        raise AssertionError(f"Unexpected URL: {url}")

    client = AsyncMock()
    client.get = fake_get

    with patch("ainvestor.collectors.derivatives.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value = client
        snaps = await collector.collect()

    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.symbol == "ETH/USDT"
    assert snap.funding_rate == pytest.approx(0.00012)
    assert snap.mark_price == pytest.approx(3500.5)
    assert snap.open_interest == pytest.approx(123456.78)
    assert snap.next_funding_time is not None


@pytest.mark.asyncio
async def test_okx_failure_falls_back_to_bybit(collector):
    async def fake_get(url, params=None):
        url = str(url)
        if "fapi.binance.com" in url:
            return _response(451, text="blocked", url=url)
        if "okx.com" in url:
            return _response(500, text="error", url=url)
        if "api.bybit.com" in url:
            return _response(
                200,
                url=url,
                json_data={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "ETHUSDT",
                                "fundingRate": "0.00008",
                                "markPrice": "3400.1",
                                "openInterest": "99999",
                                "nextFundingTime": "1784073600000",
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected URL: {url}")

    client = AsyncMock()
    client.get = fake_get

    with patch("ainvestor.collectors.derivatives.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value = client
        snaps = await collector.collect()

    assert len(snaps) == 1
    assert snaps[0].funding_rate == pytest.approx(0.00008)
    assert snaps[0].open_interest == pytest.approx(99999.0)
    assert snaps[0].mark_price == pytest.approx(3400.1)
