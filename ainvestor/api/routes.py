from __future__ import annotations

from typing import Any

from ainvestor.utils.datetime_utils import app_now_iso, format_app_datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ainvestor.config import get_profile_ai_cycle_interval, get_settings, load_risk_config
from ainvestor.cycle_runner import CycleRunner
from ainvestor.db.models import AIDecision, CycleRun, DecisionOutcome, Portfolio, Trade, get_db
from ainvestor.engine.learning import DecisionLearning
from ainvestor.portfolio.manager import PortfolioManager
from ainvestor.portfolio.profiles import PROFILE_LABELS, PROFILES, normalize_profile

router = APIRouter()


def _profile_param(profile: str = Query("extreme", alias="profile")) -> str:
    return normalize_profile(profile)


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": app_now_iso(), "timezone": "Europe/Madrid"}


@router.get("/portfolios")
async def list_portfolios(db: Session = Depends(get_db)):
    settings = get_settings()
    portfolios = (
        db.query(Portfolio)
        .filter(Portfolio.mode == settings.trading_mode)
        .order_by(Portfolio.profile.asc())
        .all()
    )
    return [
        {
            "id": p.id,
            "profile": p.profile,
            "label": PROFILE_LABELS.get(p.profile, p.profile),
            "initial_balance": p.initial_balance,
            "mode": p.mode,
        }
        for p in portfolios
    ]


@router.get("/portfolio")
async def get_portfolio(
    db: Session = Depends(get_db),
    profile: str = Depends(_profile_param),
):
    from ainvestor.services.market_prices import get_open_position_symbols, resolve_prices

    mgr = PortfolioManager(db, profile=profile)
    portfolio = mgr.get_or_create_portfolio()
    live_symbols = get_open_position_symbols(db, portfolio.id)
    prices = await resolve_prices(db, live_symbols=live_symbols)
    snapshot = await mgr.get_snapshot(prices)
    return snapshot.model_dump()


@router.get("/trades")
async def get_trades(
    limit: int = 50,
    db: Session = Depends(get_db),
    profile: str = Depends(_profile_param),
):
    from ainvestor.services.trade_labels import trade_to_api_dict

    mgr = PortfolioManager(db, profile=profile)
    trades = mgr.get_trade_history(limit=limit)
    return [trade_to_api_dict(t) for t in trades]


@router.get("/decisions")
async def get_decisions(
    limit: int = 20,
    db: Session = Depends(get_db),
    profile: str = Depends(_profile_param),
):
    decisions = (
        db.query(AIDecision)
        .filter(AIDecision.profile == profile)
        .order_by(AIDecision.created_at.desc())
        .limit(limit)
        .all()
    )
    cycle_ids = [d.cycle_id for d in decisions]
    outcomes_by_cycle: dict[str, list] = {cid: [] for cid in cycle_ids}
    if cycle_ids:
        all_outcomes = (
            db.query(DecisionOutcome)
            .filter(DecisionOutcome.cycle_id.in_(cycle_ids))
            .all()
        )
        for o in all_outcomes:
            outcomes_by_cycle.setdefault(o.cycle_id, []).append(o)

    result = []
    for d in decisions:
        outcomes = outcomes_by_cycle.get(d.cycle_id, [])
        result.append(
            {
                "cycle_id": d.cycle_id,
                "profile": d.profile,
                "model": d.model,
                "summary": d.summary or "",
                "hold": d.hold if d.hold is not None else True,
                "approved": d.approved_count,
                "rejected": d.rejected_count,
                "run_id": d.run_id,
                "created_at": format_app_datetime(d.created_at),
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
                        "instrument_type": o.instrument_type,
                        "position_side": o.position_side,
                        "leverage": o.leverage,
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
    profile: str = Depends(_profile_param),
):
    from ainvestor.services.charts import build_performance_chart

    sym = None if symbol in ("portfolio", "", "all") else symbol
    return await build_performance_chart(db, hours=hours, symbol=sym, profile=profile)


@router.get("/ai/usage")
async def get_ai_usage(
    db: Session = Depends(get_db),
    profile: str | None = Query(None, alias="profile"),
):
    """Resumen acumulado de tokens consumidos por ciclos IA."""
    from sqlalchemy import func

    filt = db.query(AIDecision)
    if profile:
        filt = filt.filter(AIDecision.profile == normalize_profile(profile))

    totals = filt.with_entities(
        func.count(AIDecision.id),
        func.coalesce(func.sum(AIDecision.tokens_input), 0),
        func.coalesce(func.sum(AIDecision.tokens_output), 0),
        func.coalesce(func.sum(AIDecision.tokens_cache_read), 0),
    ).one()

    recent_q = db.query(AIDecision)
    if profile:
        recent_q = recent_q.filter(AIDecision.profile == normalize_profile(profile))
    recent = recent_q.order_by(AIDecision.created_at.desc()).limit(10).all()

    return {
        "profile": profile,
        "cycles": int(totals[0] or 0),
        "total_input_tokens": int(totals[1] or 0),
        "total_output_tokens": int(totals[2] or 0),
        "total_cache_read_tokens": int(totals[3] or 0),
        "total_tokens": int(totals[1] or 0) + int(totals[2] or 0),
        "recent": [
            {
                "cycle_id": d.cycle_id[:8],
                "profile": d.profile,
                "created_at": format_app_datetime(d.created_at),
                "total_tokens": d.tokens_total or 0,
                "input_tokens": d.tokens_input or 0,
                "output_tokens": d.tokens_output or 0,
            }
            for d in recent
        ],
    }


@router.get("/learning")
async def get_learning(
    db: Session = Depends(get_db),
    profile: str = Depends(_profile_param),
):
    learning = DecisionLearning(db, profile=profile)
    recent = (
        db.query(DecisionOutcome)
        .filter(DecisionOutcome.profile == profile)
        .order_by(DecisionOutcome.created_at.desc())
        .limit(20)
        .all()
    )
    return {
        "profile": profile,
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
                "instrument_type": r.instrument_type,
                "position_side": r.position_side,
                "leverage": r.leverage,
                "created_at": format_app_datetime(r.created_at),
                "evaluated_at": format_app_datetime(r.evaluated_at),
            }
            for r in recent
        ],
    }


@router.get("/cycles")
async def get_cycles(
    limit: int = 20,
    db: Session = Depends(get_db),
    profile: str | None = Query(None, alias="profile"),
):
    query = db.query(CycleRun)
    if profile:
        query = query.filter(CycleRun.profile == normalize_profile(profile))
    cycles = query.order_by(CycleRun.started_at.desc()).limit(limit).all()
    return [
        {
            "cycle_id": c.cycle_id,
            "profile": c.profile,
            "status": c.status,
            "started_at": format_app_datetime(c.started_at),
            "completed_at": format_app_datetime(c.completed_at),
            "error": c.error,
        }
        for c in cycles
    ]


@router.post("/cycle/run")
async def run_cycle_manual(
    db: Session = Depends(get_db),
    profile: str | None = Query(None, alias="profile"),
):
    if profile:
        runner = CycleRunner(db, profile=normalize_profile(profile))
        return await runner.run()

    results = []
    for prof in PROFILES:
        runner = CycleRunner(db, profile=prof)
        results.append(await runner.run())
    return {"profiles": results}


@router.post("/paper/reset")
async def reset_paper_data(
    clear_market: bool = Query(False),
    db: Session = Depends(get_db),
):
    from ainvestor.services.paper_reset import reset_paper_portfolios

    reset_paper_portfolios(db, clear_market_history=clear_market)
    return {"status": "ok", "message": "Paper portfolios reset to initial balance"}


@router.post("/kill-switch/{action}")
async def toggle_kill_switch(
    action: str,
    db: Session = Depends(get_db),
    profile: str = Depends(_profile_param),
):
    if action not in ("on", "off"):
        raise HTTPException(400, "Action must be 'on' or 'off'")
    mgr = PortfolioManager(db, profile=profile)
    mgr.set_kill_switch(action == "on")
    return {
        "profile": profile,
        "kill_switch_active": action == "on",
    }


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
async def get_market_context(
    db: Session = Depends(get_db),
    fresh: bool = Query(False, alias="fresh"),
):
    """Latest market context from cache/DB; live collect only if stale or fresh=true."""
    from ainvestor.services.market_context_cache import get_market_context

    return await get_market_context(db, fresh=fresh)


@router.get("/portfolio/unified")
async def get_unified_portfolio(db: Session = Depends(get_db)):
    from ainvestor.collectors.stocks import StockCollector
    from ainvestor.portfolio.unified import UnifiedPortfolioManager
    from ainvestor.services.market_prices import resolve_prices

    prices = await resolve_prices(db)
    stock_tickers = await StockCollector().collect_all()
    stock_prices = {t.symbol: t.last for t in stock_tickers}

    unified = await UnifiedPortfolioManager(db).get_snapshot(prices, stock_prices)
    return unified.model_dump()


@router.get("/dashboard")
async def get_dashboard(
    db: Session = Depends(get_db),
    profile: str = Depends(_profile_param),
):
    """Aggregated dashboard payload (single round-trip)."""
    from ainvestor.services.market_context_cache import get_market_context

    portfolio = await get_portfolio(db=db, profile=profile)
    trades = await get_trades(limit=10, db=db, profile=profile)
    decisions = await get_decisions(limit=10, db=db, profile=profile)
    learning = await get_learning(db=db, profile=profile)
    token_usage = await get_ai_usage(db=db, profile=profile)
    market_context = await get_market_context(db, fresh=False)
    return {
        "profile": profile,
        "portfolio": portfolio,
        "trades": trades,
        "decisions": decisions,
        "learning": learning,
        "token_usage": token_usage,
        "market_context": market_context,
    }


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
async def get_config(profile: str = Depends(_profile_param)):
    settings = get_settings()
    risk = load_risk_config(profile=profile)
    from ainvestor.engine.risk import max_position_pct_for_conviction

    pos = risk["position"]
    fees = risk.get("fees", {})
    return {
        "profile": profile,
        "profile_label": PROFILE_LABELS.get(profile, profile),
        "trading_mode": settings.trading_mode,
        "ai_model": settings.effective_ai_model(),
        "ai_use_mcp": settings.ai_use_mcp,
        "cursor_configured": bool(settings.cursor_api_key.strip()),
        "cursor_model": settings.cursor_model_selection().to_json(),
        "intervals": {
            "ai_cycle": get_profile_ai_cycle_interval(profile),
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
async def get_status(
    db: Session = Depends(get_db),
    profile: str = Depends(_profile_param),
) -> dict[str, Any]:
    settings = get_settings()
    portfolio = await get_portfolio(db=db, profile=profile)
    recent_cycles = await get_cycles(limit=5, db=db, profile=profile)
    return {
        "mode": settings.trading_mode,
        "profile": profile,
        "portfolio": portfolio,
        "recent_cycles": recent_cycles,
        "scheduler_active": True,
    }
