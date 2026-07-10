from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy.orm import Session

from ainvestor.config import load_risk_config
from ainvestor.models.schemas import MarketTicker

logger = logging.getLogger(__name__)


class StockCollector:
    """Stock/ETF quotes via yfinance."""

    def __init__(self):
        self._symbols = load_risk_config().get("assets", {}).get("stocks", [])

    @property
    def symbols(self) -> list[str]:
        return self._symbols

    async def collect_all(self) -> list[MarketTicker]:
        if not self._symbols:
            return []
        try:
            import asyncio

            return await asyncio.to_thread(self._collect_sync)
        except ImportError:
            logger.warning("yfinance not installed — stock collection skipped")
            return []

    def _collect_sync(self) -> list[MarketTicker]:
        import yfinance as yf

        tickers: list[MarketTicker] = []
        for symbol in self._symbols:
            try:
                info = yf.Ticker(symbol)
                hist = info.history(period="2d")
                if hist.empty:
                    continue
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last
                change_pct = ((last - prev) / prev * 100) if prev else 0
                tickers.append(
                    MarketTicker(
                        symbol=symbol,
                        last=last,
                        volume=float(hist["Volume"].iloc[-1]) if "Volume" in hist else None,
                        change_pct=change_pct,
                        timestamp=app_now(),
                    )
                )
            except Exception as e:
                logger.warning("Stock fetch failed %s: %s", symbol, e)
        return tickers

    def summarize(self, tickers: list[MarketTicker]) -> str:
        if not tickers:
            return "No stock data (market closed or yfinance unavailable)."
        lines = []
        for t in tickers:
            chg = f"{t.change_pct:+.2f}%" if t.change_pct is not None else "N/A"
            lines.append(f"{t.symbol}: ${t.last:.2f} ({chg})")
        return "\n".join(lines)
