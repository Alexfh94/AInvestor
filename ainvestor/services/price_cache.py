"""In-memory live price cache refreshed every few seconds via batch ticker fetch."""
from __future__ import annotations

import logging
from datetime import datetime
from threading import Lock

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import get_all_market_pairs
from ainvestor.utils.datetime_utils import app_now, app_now_iso

logger = logging.getLogger(__name__)

SNAPSHOT_PERSIST_SECONDS = 60

_cache: dict[str, float] = {}
_updated_at: datetime | None = None
_last_snapshot_persist: datetime | None = None
_lock = Lock()


def get_prices(symbols: list[str] | None = None) -> dict[str, float]:
    with _lock:
        if symbols is None:
            return dict(_cache)
        return {s: _cache[s] for s in symbols if s in _cache}


def get_updated_at() -> datetime | None:
    with _lock:
        return _updated_at


def get_updated_at_iso() -> str | None:
    ts = get_updated_at()
    return ts.isoformat() if ts else None


async def refresh_prices(symbols: list[str] | None = None) -> dict[str, float]:
    """Fetch latest prices in one batch call and update the in-memory cache."""
    global _updated_at

    symbol_list = list(symbols or get_all_market_pairs())
    if not symbol_list:
        return get_prices()

    client = ExchangeClient()
    try:
        tickers = await client.fetch_tickers(symbol_list)
        now = app_now()
        with _lock:
            for sym, ticker in tickers.items():
                last = ticker.get("last") or ticker.get("close")
                if last:
                    _cache[sym] = float(last)
            _updated_at = now
    except Exception as e:
        logger.warning("price_cache batch refresh failed (%d symbols): %s", len(symbol_list), e)
        for sym in symbol_list:
            if sym in _cache:
                continue
            try:
                ticker = await client.fetch_ticker(sym)
                last = ticker.get("last") or ticker.get("close")
                if last:
                    with _lock:
                        _cache[sym] = float(last)
                        _updated_at = app_now()
            except Exception as inner:
                logger.debug("price_cache ticker %s failed: %s", sym, inner)

    return get_prices(symbol_list)


async def maybe_persist_snapshots(db, symbols: list[str]) -> None:
    """Lightweight DB persist at most once per minute to avoid SQLite bloat."""
    global _last_snapshot_persist

    now = app_now()
    if (
        _last_snapshot_persist
        and (now - _last_snapshot_persist).total_seconds() < SNAPSHOT_PERSIST_SECONDS
    ):
        return

    prices = get_prices(symbols)
    if not prices:
        return

    from ainvestor.db.models import MarketSnapshot

    for sym, price in prices.items():
        db.add(
            MarketSnapshot(
                symbol=sym,
                last_price=price,
            )
        )
    db.commit()
    _last_snapshot_persist = now
    logger.debug("Persisted %d price snapshots", len(prices))


def cache_status() -> dict:
    with _lock:
        return {
            "symbols": len(_cache),
            "updated_at": _updated_at.isoformat() if _updated_at else None,
        }
