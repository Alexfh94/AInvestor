"""Cached market context for dashboard — DB-first, live collect only when stale."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ainvestor.collectors.derivatives_store import DerivativesCollector
from ainvestor.collectors.macro import MacroCollector
from ainvestor.collectors.news import NewsCollector
from ainvestor.collectors.sentiment import SentimentCollector
from ainvestor.cycle_runner import get_last_market_context
from ainvestor.db.models import DerivativesRecord, MarketSnapshot, NewsRecord, SentimentRecord
from ainvestor.services.market_hours import market_status_label
from ainvestor.utils.datetime_utils import APP_TZ, app_now, app_now_iso

logger = logging.getLogger(__name__)

CACHE_KEY = "market_context_v1"
CACHE_TTL_MINUTES = 5


def _load_persisted_context(db: Session) -> dict | None:
    from sqlalchemy import text

    row = db.execute(
        text("SELECT value FROM app_meta WHERE key = :k"),
        {"k": CACHE_KEY},
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def persist_market_context(db: Session, context: dict) -> None:
    from sqlalchemy import text

    db.execute(
        text(
            "CREATE TABLE IF NOT EXISTS app_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
    )
    db.execute(
        text(
            "INSERT INTO app_meta (key, value) VALUES (:k, :v) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        ),
        {"k": CACHE_KEY, "v": json.dumps(context, default=str)},
    )
    db.commit()


def _context_fresh(context: dict, ttl_minutes: int = CACHE_TTL_MINUTES) -> bool:
    captured = context.get("captured_at")
    if not captured:
        return False
    try:
        if isinstance(captured, datetime):
            ts = captured.replace(tzinfo=None) if captured.tzinfo else captured
        elif isinstance(captured, str):
            dt = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            ts = (
                dt.astimezone(APP_TZ).replace(tzinfo=None)
                if dt.tzinfo
                else dt
            )
        else:
            return False
        return ts >= app_now() - timedelta(minutes=ttl_minutes)
    except Exception:
        return False


def _tickers_from_snapshots(db: Session, limit: int = 12) -> list[dict]:
    rows = (
        db.query(MarketSnapshot)
        .order_by(MarketSnapshot.captured_at.desc())
        .limit(500)
        .all()
    )
    seen: set[str] = set()
    tickers: list[dict] = []
    for row in rows:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        spread = None
        if row.bid and row.ask and row.last_price:
            spread = round((row.ask - row.bid) / row.last_price * 100, 4)
        tickers.append(
            {
                "symbol": row.symbol,
                "last": row.last_price,
                "bid": row.bid,
                "ask": row.ask,
                "volume": row.volume_24h,
                "change_pct": row.change_pct_24h,
                "spread_pct": spread,
                "timestamp": row.captured_at.isoformat() if row.captured_at else None,
            }
        )
        if len(tickers) >= limit:
            break
    return tickers


def _derivatives_from_db(db: Session) -> list[dict]:
    rows = (
        db.query(DerivativesRecord)
        .order_by(DerivativesRecord.captured_at.desc())
        .limit(200)
        .all()
    )
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        out.append(
            {
                "symbol": row.symbol,
                "funding_rate": row.funding_rate,
                "funding_rate_pct": row.funding_rate_pct,
                "mark_price": row.mark_price,
                "open_interest": row.open_interest,
                "timestamp": row.captured_at.isoformat() if row.captured_at else None,
            }
        )
    return out


def _sentiment_from_db(db: Session) -> dict:
    row = (
        db.query(SentimentRecord)
        .order_by(SentimentRecord.captured_at.desc())
        .first()
    )
    if not row:
        return {}
    return {
        "fear_greed_index": row.fear_greed_index,
        "fear_greed_label": row.fear_greed_label,
        "btc_dominance": row.btc_dominance,
        "timestamp": row.captured_at.isoformat() if row.captured_at else None,
    }


def _news_from_db(db: Session, limit: int = 10) -> list[dict]:
    rows = (
        db.query(NewsRecord)
        .order_by(NewsRecord.captured_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "title": n.title,
            "url": n.url,
            "source": n.source,
            "sentiment": n.sentiment,
            "published_at": n.published_at.isoformat() if n.published_at else None,
        }
        for n in rows
    ]


def build_context_from_db(db: Session) -> dict:
    sentiment = _sentiment_from_db(db)
    macro: dict = {}
    if sentiment.get("btc_dominance"):
        macro["btc_dominance"] = sentiment["btc_dominance"]
    tickers = _tickers_from_snapshots(db)
    return {
        "macro": macro,
        "derivatives": _derivatives_from_db(db),
        "sentiment": sentiment,
        "news": _news_from_db(db),
        "tickers": tickers,
        "signals": [],
        "market_status": market_status_label(),
        "captured_at": app_now_iso(),
        "source": "database",
    }


async def collect_live_context(db: Session) -> dict:
    import asyncio

    macro_col = MacroCollector()
    deriv_col = DerivativesCollector(db)
    sent_col = SentimentCollector(db)
    news_col = NewsCollector(db)

    macro, deriv, news = await asyncio.gather(
        macro_col.collect(),
        deriv_col.collect_and_persist(),
        news_col.collect(),
    )
    sentiment = await sent_col.collect(btc_dominance=macro.btc_dominance)
    tickers = _tickers_from_snapshots(db)
    return {
        "macro": macro.model_dump(mode="json"),
        "derivatives": [d.model_dump(mode="json") for d in deriv],
        "sentiment": sentiment.model_dump(mode="json"),
        "news": [n.model_dump(mode="json") for n in news[:10]],
        "tickers": tickers,
        "signals": [],
        "market_status": market_status_label(),
        "captured_at": app_now_iso(),
        "source": "live",
    }


async def get_market_context(db: Session, *, fresh: bool = False) -> dict:
    if not fresh:
        mem = get_last_market_context()
        if mem and _context_fresh(mem):
            return mem

        persisted = _load_persisted_context(db)
        if persisted and _context_fresh(persisted):
            return persisted

        db_ctx = build_context_from_db(db)
        if db_ctx.get("tickers") or db_ctx.get("derivatives"):
            return db_ctx

    ctx = await collect_live_context(db)
    persist_market_context(db, ctx)
    return ctx
