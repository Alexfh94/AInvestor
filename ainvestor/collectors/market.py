from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy.orm import Session

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import load_risk_config
from ainvestor.models.schemas import MarketTicker

logger = logging.getLogger(__name__)

TIMEFRAMES = ("1h", "4h", "1d")


class MarketCollector:
    """Fetches market data from exchange via ccxt."""

    def __init__(self, db: Session, exchange_id: str | None = None):
        self.db = db
        self.client = ExchangeClient(exchange_id=exchange_id)
        self._pairs = load_risk_config()["whitelist"]["pairs"]

    @property
    def pairs(self) -> list[str]:
        return self._pairs

    async def collect_all(self) -> list[MarketTicker]:
        tickers: list[MarketTicker] = []
        for symbol in self._pairs:
            try:
                ticker = await self.collect_symbol(symbol)
                tickers.append(ticker)
            except Exception as e:
                logger.warning("Failed to collect %s: %s", symbol, e)
        return tickers

    async def collect_symbol(self, symbol: str) -> MarketTicker:
        import json

        from ainvestor.db.models import MarketSnapshot

        raw = await self.client.fetch_ticker(symbol)
        ohlcv = await self.client.fetch_ohlcv(symbol, timeframe="1h", limit=100)
        spread_pct = await self._estimate_spread(symbol, raw)

        ticker = MarketTicker(
            symbol=symbol,
            last=raw.get("last") or raw.get("close", 0),
            bid=raw.get("bid"),
            ask=raw.get("ask"),
            volume=raw.get("quoteVolume") or raw.get("baseVolume"),
            change_pct=raw.get("percentage"),
            spread_pct=spread_pct,
            timestamp=app_now(),
        )

        snapshot = MarketSnapshot(
            symbol=symbol,
            last_price=ticker.last,
            bid=ticker.bid,
            ask=ticker.ask,
            volume_24h=ticker.volume,
            change_pct_24h=ticker.change_pct,
            ohlcv_json=json.dumps(ohlcv),
            captured_at=ticker.timestamp,
        )
        self.db.add(snapshot)
        self.db.commit()

        return ticker

    async def _estimate_spread(self, symbol: str, ticker: dict) -> float | None:
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        last = ticker.get("last") or ticker.get("close")
        if bid and ask and last and last > 0:
            return round((ask - bid) / last * 100, 4)
        try:
            book = await self.client.fetch_order_book(symbol, limit=5)
            best_bid = book["bids"][0][0] if book.get("bids") else None
            best_ask = book["asks"][0][0] if book.get("asks") else None
            if best_bid and best_ask and last and last > 0:
                return round((best_ask - best_bid) / last * 100, 4)
        except Exception as e:
            logger.debug("Order book spread failed for %s: %s", symbol, e)
        return None

    async def get_latest_ohlcv(self, symbol: str, limit: int = 100) -> list[list]:
        return await self.client.fetch_ohlcv(symbol, timeframe="1h", limit=limit)

    async def get_multi_timeframe_ohlcv(
        self, symbol: str, limit: int = 100
    ) -> dict[str, list[list]]:
        data: dict[str, list[list]] = {}
        for tf in TIMEFRAMES:
            try:
                data[tf] = await self.client.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            except Exception as e:
                logger.warning("OHLCV %s %s failed: %s", symbol, tf, e)
        return data

    async def collect_all_multi_timeframe(self) -> dict[str, dict[str, list[list]]]:
        result: dict[str, dict[str, list[list]]] = {}
        for symbol in self._pairs:
            result[symbol] = await self.get_multi_timeframe_ohlcv(symbol)
        return result

    def get_recent_snapshots(self, symbol: str, limit: int = 10):
        from ainvestor.db.models import MarketSnapshot

        return (
            self.db.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == symbol)
            .order_by(MarketSnapshot.captured_at.desc())
            .limit(limit)
            .all()
        )
