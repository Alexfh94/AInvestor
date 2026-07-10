from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import get_settings, load_risk_config
from ainvestor.db.models import MarketSnapshot
from ainvestor.models.schemas import MarketTicker

logger = logging.getLogger(__name__)


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
        raw = await self.client.fetch_ticker(symbol)
        ohlcv = await self.client.fetch_ohlcv(symbol, timeframe="1h", limit=100)

        ticker = MarketTicker(
            symbol=symbol,
            last=raw.get("last") or raw.get("close", 0),
            bid=raw.get("bid"),
            ask=raw.get("ask"),
            volume=raw.get("quoteVolume") or raw.get("baseVolume"),
            change_pct=raw.get("percentage"),
            timestamp=datetime.utcnow(),
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

    async def get_latest_ohlcv(self, symbol: str, limit: int = 100) -> list[list]:
        return await self.client.fetch_ohlcv(symbol, timeframe="1h", limit=limit)

    def get_recent_snapshots(self, symbol: str, limit: int = 10) -> list[MarketSnapshot]:
        return (
            self.db.query(MarketSnapshot)
            .filter(MarketSnapshot.symbol == symbol)
            .order_by(MarketSnapshot.captured_at.desc())
            .limit(limit)
            .all()
        )
