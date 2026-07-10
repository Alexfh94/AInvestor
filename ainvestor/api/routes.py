from __future__ import annotations

from typing import Any

from ainvestor.utils.datetime_utils import app_now_iso

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ainvestor.config import get_settings, load_risk_config
from ainvestor.cycle_runner import CycleRunner
from ainvestor.db.models import AIDecision, CycleRun, DecisionOutcome, Trade, get_db
from ainvestor.engine.learning import DecisionLearning
from ainvestor.portfolio.manager import PortfolioManager

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": app_now_iso(), "timezone": "Europe/Madrid"}


@router.get("/portfolio")
async def get_portfolio(db: Session = Depends(get_db)):
    mgr = PortfolioManager(db)
    from ainvestor.collectors.market import MarketCollector

    collector = MarketCollector(db)
    prices: dict[str, float] = {}
    for symbol in collector.pairs:
        try:
            ticker = await collector.client.fetch_ticker(symbol)
            prices[symbol] = ticker.get("last") or ticker.get("close", 0)
        except Exception:
            pass

    snapshot = await mgr.get_snapshot(prices)
    return snapshot.model_dump()


@router.get("/trades")
async def get_trades(limit: int = 50, db: Session = Depends(get_db)):
    mgr = PortfolioManager(db)
    trades = mgr.get_trade_history(limit=limit)
    return [
        {
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "amount": t.amount,
            "price": t.price,
            "value_usdt": t.value_usdt,
            "fee": t.fee,
            "mode": t.mode,
            "status": t.status,
            "executed_at": t.executed_at.isoformat(),
        }
        for t in trades
    ]


@router.get("/decisions")
async def get_decisions(limit: int = 20, db: Session = Depends(get_db)):
    decisions = (
        db.query(AIDecision)
        .order_by(AIDecision.created_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for d in decisions:
        outcomes = (
            db.query(DecisionOutcome)
            .filter(DecisionOutcome.cycle_id == d.cycle_id)
            .all()
        )
        result.append(
            {
                "cycle_id": d.cycle_id,
                "model": d.model,
                "summary": d.summary or "",
                "hold": d.hold if d.hold is not None else True,
                "approved": d.approved_count,
                "rejected": d.rejected_count,
                "run_id": d.run_id,
                "created_at": d.created_at.isoformat(),
                "token_usage": {
                    "input_tokens": d.tokens_input or 0,
                    "output_tokens": d.tokens_output or 0,
                    "cache_read_tokens": d.tokens_cache_read or 0,
                    "cache_write_tokens": d.tokens_cache_write or 0,
                    "total_tokens": d.tokens_total or 0,
                },
                "outcomes": [
                    {
                        "symbol": o.symbol,
                        "action": o.action,
                        "execution_status": o.execution_status,
                        "outcome": o.outcome,
                        "return_pct": o.return_pct,
                        "notes": o.outcome_notes,
                    }
                    for o in outcomes
                ],
            }
        )
    return result


@router.get("/charts/performance")
async def get_performance_chart(
    hours: int = 48,
    symbol: str = "portfolio",
    db: Session = Depends(get_db),
):
    from ainvestor.services.charts import build_performance_chart

    sym = None if symbol in ("portfolio", "", "all") else symbol
    return await build_performance_chart(db, hours=hours, symbol=sym)


@router.get("/ai/usage")
async def get_ai_usage(db: Session = Depends(get_db)):
    """Resumen acumulado de tokens consumidos por ciclos IA."""
    decisions = db.query(AIDecision).all()
    total_in = sum(d.tokens_input or 0 for d in decisions)
    total_out = sum(d.tokens_output or 0 for d in decisions)
    total_cache = sum(d.tokens_cache_read or 0 for d in decisions)
    return {
        "cycles": len(decisions),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cache_read_tokens": total_cache,
        "total_tokens": total_in + total_out,
        "recent": [
            {
                "cycle_id": d.cycle_id[:8],
                "created_at": d.created_at.isoformat(),
                "total_tokens": d.tokens_total or 0,
                "input_tokens": d.tokens_input or 0,
                "output_tokens": d.tokens_output or 0,
            }
            for d in sorted(decisions, key=lambda x: x.created_at, reverse=True)[:10]
        ],
    }


@router.get("/learning")
async def get_learning(db: Session = Depends(get_db)):
    learning = DecisionLearning(db)
    learning.backfill_from_decisions()
    recent = (
        db.query(DecisionOutcome)
        .order_by(DecisionOutcome.created_at.desc())
        .limit(20)
        .all()
    )
    return {
        "stats": learning.get_stats(),
        "summary": learning.build_learning_summary(),
        "recent": [
            {
                "cycle_id": r.cycle_id,
                "symbol": r.symbol,
                "action": r.action,
                "execution_status": r.execution_status,
                "outcome": r.outcome,
                "return_pct": r.return_pct,
                "summary": r.summary,
                "reasoning": r.reasoning,
                "notes": r.outcome_notes,
                "created_at": r.created_at.isoformat(),
                "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
            }
            for r in recent
        ],
    }


@router.get("/cycles")
async def get_cycles(limit: int = 20, db: Session = Depends(get_db)):
    cycles = (
        db.query(CycleRun)
        .order_by(CycleRun.started_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "cycle_id": c.cycle_id,
            "status": c.status,
            "started_at": c.started_at.isoformat(),
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
            "error": c.error,
        }
        for c in cycles
    ]


@router.post("/cycle/run")
async def run_cycle_manual(db: Session = Depends(get_db)):
    runner = CycleRunner(db)
    result = await runner.run()
    return result


@router.post("/kill-switch/{action}")
async def toggle_kill_switch(action: str, db: Session = Depends(get_db)):
    if action not in ("on", "off"):
        raise HTTPException(400, "Action must be 'on' or 'off'")
    mgr = PortfolioManager(db)
    mgr.set_kill_switch(action == "on")
    return {"kill_switch_active": action == "on"}


@router.get("/testnet/status")
async def testnet_status():
    from ainvestor.engine.testnet import validate_testnet_credentials

    return validate_testnet_credentials()


@router.get("/dex/status")
async def dex_status():
    from ainvestor.dex import DexConnector

    dex = DexConnector()
    return {"enabled": dex.is_enabled, "phase": "8-future"}


@router.get("/market/context")
async def get_market_context(db: Session = Depends(get_db)):
    """Latest market context from last cycle or live collection."""
    from ainvestor.cycle_runner import get_last_market_context
    from ainvestor.collectors.macro import MacroCollector
    from ainvestor.collectors.derivatives_store import DerivativesCollector
    from ainvestor.collectors.sentiment import SentimentCollector
    from ainvestor.collectors.news import NewsCollector
    from ainvestor.services.market_hours import market_status_label

    cached = get_last_market_context()
    if cached:
        return cached

    macro = await MacroCollector().collect()
    deriv = await DerivativesCollector(db).collect_and_persist()
    sentiment = await SentimentCollector(db).collect(btc_dominance=macro.btc_dominance)
    news = await NewsCollector(db).collect()

    return {
        "macro": macro.model_dump(mode="json"),
        "derivatives": [d.model_dump(mode="json") for d in deriv],
        "sentiment": sentiment.model_dump(mode="json"),
        "news": [n.model_dump(mode="json") for n in news[:10]],
        "market_status": market_status_label(),
        "captured_at": app_now_iso(),
    }


@router.get("/portfolio/unified")
async def get_unified_portfolio(db: Session = Depends(get_db)):
    from ainvestor.collectors.market import MarketCollector
    from ainvestor.collectors.stocks import StockCollector
    from ainvestor.portfolio.unified import UnifiedPortfolioManager

    crypto_collector = MarketCollector(db)
    prices: dict[str, float] = {}
    for symbol in crypto_collector.pairs:
        try:
            ticker = await crypto_collector.client.fetch_ticker(symbol)
            prices[symbol] = ticker.get("last") or ticker.get("close", 0)
        except Exception:
            pass

    stock_tickers = await StockCollector().collect_all()
    stock_prices = {t.symbol: t.last for t in stock_tickers}

    unified = await UnifiedPortfolioManager(db).get_snapshot(prices, stock_prices)
    return unified.model_dump()


@router.get("/ibkr/status")
async def ibkr_status():
    from ainvestor.brokers.ibkr import IBKRBroker

    broker = IBKRBroker()
    return {
        "enabled": broker.enabled,
        "connected": broker._connected,
        "host": broker.risk.get("host"),
        "port": broker.risk.get("port"),
        "paper": broker.risk.get("paper", True),
    }


@router.post("/ibkr/sync")
async def ibkr_sync(db: Session = Depends(get_db)):
    from ainvestor.brokers.ibkr import IBKRBroker

    broker = IBKRBroker()
    if not await broker.connect():
        raise HTTPException(503, "IBKR not available")
    try:
        count = await broker.sync_positions_to_db(db)
        positions = await broker.get_positions()
        summary = await broker.get_account_summary()
        return {"synced": count, "positions": positions, "account": summary}
    finally:
        await broker.disconnect()


@router.get("/config")
async def get_config():
    settings = get_settings()
    risk = load_risk_config()
    from ainvestor.engine.risk import max_position_pct_for_conviction

    pos = risk["position"]
    fees = risk.get("fees", {})
    return {
        "trading_mode": settings.trading_mode,
        "ai_model": settings.ai_model,
        "intervals": {
            "ai_cycle": settings.ai_cycle_interval,
            "risk_monitor": settings.risk_monitor_interval,
            "market_collect": settings.market_collect_interval,
        },
        "risk": risk,
        "position_sizing": {
            "conviction_50_pct": round(max_position_pct_for_conviction(50, risk), 1),
            "conviction_70_pct": round(max_position_pct_for_conviction(70, risk), 1),
            "conviction_90_pct": round(max_position_pct_for_conviction(90, risk), 1),
            "max_high_conviction": pos.get("max_position_pct_high_conviction"),
            "min_order_usdt": pos["min_order_value_usdt"],
        },
        "fees": {
            "exchange": fees.get("exchange", settings.default_exchange),
            "fallback_taker_pct": fees.get("fallback_taker_rate", 0.001) * 100,
        },
    }


@router.get("/status")
async def get_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    portfolio = await get_portfolio(db)
    recent_cycles = await get_cycles(limit=5, db=db)
    return {
        "mode": settings.trading_mode,
        "portfolio": portfolio,
        "recent_cycles": recent_cycles,
        "scheduler_active": True,
    }
