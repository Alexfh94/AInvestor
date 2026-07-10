from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from ainvestor.utils.datetime_utils import app_now_iso

from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.config import get_settings, load_risk_config
from ainvestor.collectors.market import MarketCollector
from ainvestor.collectors.news import NewsCollector
from ainvestor.collectors.sentiment import SentimentCollector
from ainvestor.engine.quant import QuantEngine
from ainvestor.portfolio.manager import PortfolioManager

logger = logging.getLogger(__name__)
mcp = FastMCP("ainvestor-tools")

_settings = get_settings()
_engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in _settings.database_url else {},
)
SessionLocal = sessionmaker(bind=_engine)


def _get_db():
    return SessionLocal()


@mcp.tool()
def get_portfolio_state() -> str:
    """Get current portfolio: balance, positions, P&L, mode, kill switch status."""
    import asyncio

    db = _get_db()
    try:
        mgr = PortfolioManager(db)
        collector = MarketCollector(db)
        prices: dict[str, float] = {}

        async def fetch_prices():
            for symbol in collector.pairs:
                try:
                    ticker = await collector.client.fetch_ticker(symbol)
                    prices[symbol] = ticker.get("last") or ticker.get("close", 0)
                except Exception:
                    pass

        asyncio.get_event_loop().run_until_complete(fetch_prices())
        snapshot = asyncio.get_event_loop().run_until_complete(
            mgr.get_snapshot(prices)
        )
        return json.dumps(snapshot.model_dump(), default=str)
    finally:
        db.close()


@mcp.tool()
def get_market_data(symbols: str = "") -> str:
    """Get OHLCV and ticker data. symbols: comma-separated e.g. BTC/USDT,ETH/USDT"""
    import asyncio

    db = _get_db()
    try:
        collector = MarketCollector(db)
        target = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else collector.pairs
        result: dict[str, Any] = {}

        async def fetch():
            for symbol in target[:10]:
                try:
                    ticker = await collector.client.fetch_ticker(symbol)
                    ohlcv = await collector.client.fetch_ohlcv(symbol, limit=24)
                    result[symbol] = {
                        "last": ticker.get("last"),
                        "change_pct": ticker.get("percentage"),
                        "volume": ticker.get("quoteVolume"),
                        "ohlcv_count": len(ohlcv),
                        "recent_closes": [c[4] for c in ohlcv[-5:]],
                    }
                except Exception as e:
                    result[symbol] = {"error": str(e)}

        asyncio.get_event_loop().run_until_complete(fetch())
        return json.dumps(result)
    finally:
        db.close()


@mcp.tool()
def get_technical_signals(symbols: str = "") -> str:
    """Get RSI, MA, MACD signals. symbols: comma-separated or empty for all whitelist."""
    import asyncio

    db = _get_db()
    try:
        collector = MarketCollector(db)
        quant = QuantEngine()
        target = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else collector.pairs
        signals = []

        async def analyze():
            for symbol in target[:10]:
                try:
                    ohlcv = await collector.get_latest_ohlcv(symbol)
                    sig = quant.analyze(symbol, ohlcv)
                    signals.append(sig.model_dump())
                except Exception as e:
                    signals.append({"symbol": symbol, "error": str(e)})

        asyncio.get_event_loop().run_until_complete(analyze())
        return json.dumps(signals)
    finally:
        db.close()


@mcp.tool()
def get_news_summary() -> str:
    """Get latest crypto news summary."""
    import asyncio

    collector = NewsCollector()
    items = asyncio.get_event_loop().run_until_complete(collector.collect())
    return collector.summarize(items)


@mcp.tool()
def get_sentiment() -> str:
    """Get fear/greed index and Reddit sentiment."""
    import asyncio

    collector = SentimentCollector()
    data = asyncio.get_event_loop().run_until_complete(collector.collect())
    return collector.summarize(data)


@mcp.tool()
def get_trade_history(limit: int = 20) -> str:
    """Get recent trade history."""
    db = _get_db()
    try:
        mgr = PortfolioManager(db)
        trades = mgr.get_trade_history(limit=limit)
        return json.dumps(
            [
                {
                    "symbol": t.symbol,
                    "side": t.side,
                    "amount": t.amount,
                    "price": t.price,
                    "value_usdt": t.value_usdt,
                    "executed_at": t.executed_at.isoformat(),
                }
                for t in trades
            ]
        )
    finally:
        db.close()


@mcp.tool()
def get_risk_rules() -> str:
    """Get active risk management rules."""
    return json.dumps(load_risk_config())


@mcp.tool()
def propose_trade(
    action: str,
    symbol: str,
    amount_pct: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    conviction: int,
    reasoning: str,
) -> str:
    """Register a trade proposal (does NOT execute). Returns proposal JSON for validation."""
    proposal = {
        "action": action,
        "symbol": symbol,
        "amount_pct": amount_pct,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "conviction": conviction,
        "reasoning": reasoning,
        "proposed_at": app_now_iso(),
        "note": "This is a proposal only. Execution requires RiskManager approval.",
    }
    return json.dumps(proposal)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
