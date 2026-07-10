from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ainvestor.config import get_settings
from ainvestor.db.models import MarketSnapshot, Portfolio, PortfolioValueHistory, Trade
from ainvestor.portfolio.manager import PortfolioManager


def record_portfolio_value(
    db: Session, mgr: PortfolioManager, prices: dict[str, float]
) -> None:
    """Sync wrapper — use record_portfolio_value_async from async code."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(record_portfolio_value_async(db, mgr, prices))
        return
    loop.create_task(record_portfolio_value_async(db, mgr, prices))


async def record_portfolio_value_async(
    db: Session, mgr: PortfolioManager, prices: dict[str, float]
) -> None:
    snapshot = await mgr.get_snapshot(prices)
    portfolio = mgr.get_or_create_portfolio()
    db.add(
        PortfolioValueHistory(
            portfolio_id=portfolio.id,
            total_value_usdt=snapshot.total_value_usdt,
            quote_balance=snapshot.quote_balance,
            invested_usdt=snapshot.invested_usdt,
            captured_at=datetime.utcnow(),
        )
    )
    db.commit()


async def build_performance_chart(
    db: Session,
    hours: int = 48,
    symbol: str | None = None,
) -> dict:
    """Construye datos para gráfica de cartera o de un activo."""
    settings = get_settings()
    mgr = PortfolioManager(db)
    portfolio = mgr.get_or_create_portfolio()
    since = datetime.utcnow() - timedelta(hours=hours)

    trades = (
        db.query(Trade)
        .filter(Trade.portfolio_id == portfolio.id, Trade.executed_at >= since)
        .order_by(Trade.executed_at.asc())
        .all()
    )

    markers = [
        {
            "t": t.executed_at.isoformat(),
            "symbol": t.symbol,
            "side": t.side,
            "price": t.price,
            "value_usdt": t.value_usdt,
            "amount": t.amount,
        }
        for t in trades
        if not symbol or t.symbol == symbol
    ]

    if symbol and symbol != "portfolio":
        return await _asset_chart(db, symbol, since, markers, portfolio)

    return await _portfolio_chart(db, mgr, portfolio, since, markers, settings)


async def _portfolio_chart(
    db: Session,
    mgr: PortfolioManager,
    portfolio: Portfolio,
    since: datetime,
    markers: list[dict],
    settings,
) -> dict:
    history = (
        db.query(PortfolioValueHistory)
        .filter(
            PortfolioValueHistory.portfolio_id == portfolio.id,
            PortfolioValueHistory.captured_at >= since,
        )
        .order_by(PortfolioValueHistory.captured_at.asc())
        .all()
    )

    series: list[dict] = []
    if not history:
        series.append(
            {
                "t": portfolio.created_at.isoformat(),
                "value": settings.paper_initial_balance,
            }
        )
    else:
        series = [
            {"t": h.captured_at.isoformat(), "value": h.total_value_usdt}
            for h in history
        ]

    from ainvestor.collectors.market import MarketCollector

    collector = MarketCollector(db)
    prices: dict[str, float] = {}
    for pair in collector.pairs:
        try:
            ticker = await collector.client.fetch_ticker(pair)
            prices[pair] = ticker.get("last") or ticker.get("close", 0)
        except Exception:
            pass

    snapshot = await mgr.get_snapshot(prices)
    now = datetime.utcnow().isoformat()
    if not series or series[-1]["t"] != now:
        series.append({"t": now, "value": snapshot.total_value_usdt})

    initial = settings.paper_initial_balance
    current = snapshot.total_value_usdt
    return_pct = ((current - initial) / initial * 100) if initial else 0.0

    return {
        "mode": "portfolio",
        "label": "Valor cartera (USDT)",
        "series": series,
        "markers": markers,
        "symbols": collector.pairs,
        "summary": {
            "initial_usdt": initial,
            "current_usdt": current,
            "return_pct": round(return_pct, 2),
        },
    }


async def _asset_chart(
    db: Session,
    symbol: str,
    since: datetime,
    markers: list[dict],
    portfolio: Portfolio,
) -> dict:
    snapshots = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.symbol == symbol, MarketSnapshot.captured_at >= since)
        .order_by(MarketSnapshot.captured_at.asc())
        .all()
    )

    series: list[dict] = []
    if snapshots:
        first_price = snapshots[0].last_price
        for s in _downsample_snapshots(snapshots, max_points=120):
            perf = ((s.last_price - first_price) / first_price * 100) if first_price else 0
            series.append(
                {
                    "t": s.captured_at.isoformat(),
                    "value": s.last_price,
                    "performance_pct": round(perf, 2),
                }
            )
    else:
        from ainvestor.collectors.exchange_client import ExchangeClient

        client = ExchangeClient()
        try:
            ticker = await client.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close", 0)
            series.append({"t": datetime.utcnow().isoformat(), "value": price, "performance_pct": 0})
        except Exception:
            pass

    asset = symbol.split("/")[0] if "/" in symbol else symbol
    return {
        "mode": "asset",
        "symbol": symbol,
        "label": f"Precio {asset} (USDT)",
        "series": series,
        "markers": markers,
        "symbols": [symbol],
        "summary": {},
    }


def _downsample_snapshots(snapshots: list, max_points: int = 120) -> list:
    if len(snapshots) <= max_points:
        return snapshots
    step = len(snapshots) // max_points
    return snapshots[:: max(step, 1)][:max_points]
