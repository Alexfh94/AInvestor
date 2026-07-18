from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import ccxt

from ainvestor.config import get_settings

logger = logging.getLogger(__name__)

# Binance.com blocks US cloud IPs (e.g. GCP us-east1); spot price fallbacks keep same pair symbols.
PRICE_FALLBACK_EXCHANGES = ("binanceus", "coinbase", "kraken")


class ExchangeClient:
    """Unified exchange client via ccxt."""

    def __init__(self, exchange_id: str | None = None, testnet: bool = False):
        settings = get_settings()
        self.exchange_id = exchange_id or settings.default_exchange
        self.testnet = testnet
        self._exchange: ccxt.Exchange | None = None
        self._fallback_exchanges: dict[str, ccxt.Exchange] = {}

    def _get_fallback_exchange(self, exchange_id: str) -> ccxt.Exchange:
        if exchange_id not in self._fallback_exchanges:
            exchange_class = getattr(ccxt, exchange_id)
            self._fallback_exchanges[exchange_id] = exchange_class(
                {"enableRateLimit": True, "options": {"defaultType": "spot"}}
            )
        return self._fallback_exchanges[exchange_id]

    async def _fetch_with_fallback(self, op: str, symbol: str, fetcher):
        try:
            return await fetcher(self.exchange)
        except Exception as primary_err:
            if self.exchange_id != "binance":
                raise primary_err
            for fb_id in PRICE_FALLBACK_EXCHANGES:
                try:
                    fb = self._get_fallback_exchange(fb_id)
                    result = await fetcher(fb)
                    logger.info(
                        "Using %s fallback for %s %s (binance blocked/unavailable)",
                        fb_id,
                        op,
                        symbol,
                    )
                    return result
                except Exception as fb_err:
                    logger.debug("Fallback %s %s %s: %s", fb_id, op, symbol, fb_err)
            raise primary_err

    def _build_exchange(self) -> ccxt.Exchange:
        settings = get_settings()
        exchange_class = getattr(ccxt, self.exchange_id)

        config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }

        if self.exchange_id == "binance":
            config["apiKey"] = settings.binance_api_key
            config["secret"] = settings.binance_api_secret
            if self.testnet:
                config["sandbox"] = True
                config["urls"] = {
                    "api": {
                        "public": "https://testnet.binance.vision/api",
                        "private": "https://testnet.binance.vision/api",
                    }
                }
        elif self.exchange_id == "kraken":
            config["apiKey"] = settings.kraken_api_key
            config["secret"] = settings.kraken_api_secret

        return exchange_class(config)

    @property
    def exchange(self) -> ccxt.Exchange:
        if self._exchange is None:
            self._exchange = self._build_exchange()
        return self._exchange

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._fetch_with_fallback(
            "ticker",
            symbol,
            lambda ex: asyncio.to_thread(ex.fetch_ticker, symbol),
        )

    async def fetch_tickers(self, symbols: list[str]) -> dict[str, Any]:
        return await self._fetch_with_fallback(
            "tickers",
            ",".join(symbols[:3]),
            lambda ex: asyncio.to_thread(ex.fetch_tickers, symbols),
        )

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100
    ) -> list[list]:
        return await self._fetch_with_fallback(
            "ohlcv",
            symbol,
            lambda ex: asyncio.to_thread(ex.fetch_ohlcv, symbol, timeframe, None, limit),
        )

    async def fetch_order_book(self, symbol: str, limit: int = 10) -> dict[str, Any]:
        return await asyncio.to_thread(self.exchange.fetch_order_book, symbol, limit)

    async def create_market_order(
        self, symbol: str, side: str, amount: float
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.exchange.create_market_order, symbol, side, amount
        )

    async def fetch_balance(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.exchange.fetch_balance)

    async def load_markets(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.exchange.load_markets)

    async def get_taker_fee_rate(self, symbol: str) -> float:
        """Comisión taker del exchange para el par (órdenes market)."""
        settings = get_settings()
        from ainvestor.config import load_risk_config

        fallback = load_risk_config().get("fees", {}).get("fallback_taker_rate", 0.001)

        try:
            await self.load_markets()
            market = self.exchange.markets.get(symbol, {})
            taker = market.get("taker")
            if taker is not None:
                return float(taker)
            trading = getattr(self.exchange, "fees", {}).get("trading", {})
            if trading.get("taker") is not None:
                return float(trading["taker"])
        except Exception as e:
            logger.warning("Fee lookup failed for %s: %s — using fallback", symbol, e)

        return float(fallback)


class FuturesExchangeClient(ExchangeClient):
    """Binance USDT-M futures via ccxt (read-only). Prefer DerivativesCollector for funding/OI fallbacks."""

    def _build_exchange(self) -> ccxt.Exchange:
        exchange_class = getattr(ccxt, self.exchange_id)
        settings = get_settings()
        config: dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if self.exchange_id == "binance":
            config["apiKey"] = settings.binance_api_key
            config["secret"] = settings.binance_api_secret
        return exchange_class(config)
