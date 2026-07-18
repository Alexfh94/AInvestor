"""Resolve crypto prices from DB snapshots with minimal live exchange calls."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import get_all_market_pairs
from ainvestor.db.models import MarketSnapshot, Position
from ainvestor.utils.datetime_utils import app_now

logger = logging.getLogger(__name__)

STALE_MINUTES = 20


def get_open_position_symbols(db: Session, portfolio_id: int | None = None) -> set[str]:
    q = db.query(Position.symbol).filter(Position.is_open == True)  # noqa: E712
    if portfolio_id is not None:
        q = q.filter(Position.portfolio_id == portfolio_id)
    return {row[0] for row in q.distinct().all()}


def get_latest_snapshot_prices(
    db: Session, symbols: list[str]
) -> tuple[dict[str, float], dict[str, object]]:
    """Latest price per symbol from market_snapshots."""
    if not symbols:
        return {}, {}

    rows = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.symbol.in_(symbols))
        .order_by(MarketSnapshot.captured_at.desc())
        .all()
    )
    prices: dict[str, float] = {}
    captured_at: dict[str, object] = {}
    for row in rows:
        if row.symbol in prices:
            continue
        prices[row.symbol] = row.last_price
        captured_at[row.symbol] = row.captured_at
    return prices, captured_at


async def resolve_prices(
    db: Session,
    symbols: list[str] | None = None,
    *,
    live_symbols: set[str] | None = None,
    stale_minutes: int = STALE_MINUTES,
) -> dict[str, float]:
    """
    Prices for valuation: DB snapshots first; live batch fetch only for
    open positions or symbols with stale/missing snapshots.
    """
    symbol_list = symbols or get_all_market_pairs()
    live_symbols = live_symbols or set()
    prices, captured_at = get_latest_snapshot_prices(db, symbol_list)
    cutoff = app_now() - timedelta(minutes=stale_minutes)
    need_live: set[str] = set()

    for sym in symbol_list:
        if sym in live_symbols:
            need_live.add(sym)
            continue
        ts = captured_at.get(sym)
        if sym not in prices or ts is None or ts < cutoff:
            need_live.add(sym)

    if need_live:
        client = ExchangeClient()
        try:
            tickers = await client.fetch_tickers(list(need_live))
            for sym, ticker in tickers.items():
                last = ticker.get("last") or ticker.get("close")
                if last:
                    prices[sym] = float(last)
        except Exception as e:
            logger.warning("Live ticker batch failed (%s symbols): %s", len(need_live), e)
            for sym in need_live:
                if sym in prices:
                    continue
                try:
                    ticker = await client.fetch_ticker(sym)
                    last = ticker.get("last") or ticker.get("close")
                    if last:
                        prices[sym] = float(last)
                except Exception as inner:
                    logger.debug("Live ticker %s failed: %s", sym, inner)

    return prices
