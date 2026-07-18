"""Reset paper portfolios and trading/AI history for a clean benchmark run."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ainvestor.config import get_profile_initial_balance, get_settings
from ainvestor.db.models import (
    AIDecision,
    CycleRun,
    DecisionOutcome,
    Portfolio,
    PortfolioValueHistory,
    Position,
    RiskEvent,
    Trade,
)
from ainvestor.portfolio.profiles import PROFILE_AGGRESSIVE, PROFILE_CONSERVATIVE, PROFILES
from ainvestor.utils.datetime_utils import app_now

logger = logging.getLogger(__name__)

TRADING_TABLES = (
    Position,
    Trade,
    AIDecision,
    DecisionOutcome,
    CycleRun,
    RiskEvent,
    PortfolioValueHistory,
)


def reset_paper_portfolios(db: Session, *, clear_market_history: bool = False) -> dict:
    """
    Reset all profile portfolios to initial balance and wipe trading/AI records.
    Fixes any cycle_runs stuck in 'running' by deleting them.
    """
    settings = get_settings()
    counts: dict[str, int] = {}

    for model in TRADING_TABLES:
        n = db.query(model).delete(synchronize_session=False)
        counts[model.__tablename__] = n

    if clear_market_history:
        from ainvestor.db.models import DerivativesRecord, MarketSnapshot, NewsRecord, SentimentRecord

        for model in (MarketSnapshot, NewsRecord, SentimentRecord, DerivativesRecord):
            n = db.query(model).delete(synchronize_session=False)
            counts[model.__tablename__] = n

    portfolios = (
        db.query(Portfolio)
        .filter(Portfolio.mode == settings.trading_mode)
        .all()
    )
    reset_profiles: list[str] = []
    for port in portfolios:
        initial = port.initial_balance or get_profile_initial_balance(port.profile)
        port.quote_balance = initial
        port.initial_balance = initial
        port.realized_pnl = 0.0
        port.kill_switch_active = False
        port.updated_at = app_now()
        reset_profiles.append(port.profile)

    for profile in PROFILES:
        exists = any(p.profile == profile for p in portfolios)
        if not exists:
            initial = get_profile_initial_balance(profile)
            db.add(
                Portfolio(
                    mode=settings.trading_mode,
                    profile=profile,
                    quote_balance=initial,
                    initial_balance=initial,
                    quote_currency=settings.paper_quote_currency,
                )
            )
            reset_profiles.append(profile)

    db.commit()
    logger.info("Paper reset complete: %s", counts)
    return {
        "status": "ok",
        "profiles_reset": sorted(set(reset_profiles)),
        "deleted_rows": counts,
    }


def remove_conservative_portfolio(db: Session) -> dict:
    """Delete conservative portfolio and all rows tied to profile or portfolio id."""
    counts: dict[str, int] = {}
    conservative = (
        db.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_CONSERVATIVE)
        .first()
    )
    if not conservative:
        return {"status": "ok", "deleted_rows": counts, "message": "no conservative portfolio"}

    pid = conservative.id
    for model in TRADING_TABLES:
        if hasattr(model, "portfolio_id"):
            n = (
                db.query(model)
                .filter(model.portfolio_id == pid)
                .delete(synchronize_session=False)
            )
            counts[model.__tablename__] = counts.get(model.__tablename__, 0) + n
        if hasattr(model, "profile"):
            n = (
                db.query(model)
                .filter(model.profile == PROFILE_CONSERVATIVE)
                .delete(synchronize_session=False)
            )
            counts[model.__tablename__] = counts.get(model.__tablename__, 0) + n

    db.delete(conservative)
    counts["portfolios"] = 1
    db.commit()
    logger.info("Conservative portfolio removed: %s", counts)
    return {"status": "ok", "deleted_rows": counts}


def remove_aggressive_portfolio(db: Session) -> dict:
    """Delete aggressive portfolio and all rows tied to profile or portfolio id."""
    counts: dict[str, int] = {}
    aggressive = (
        db.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_AGGRESSIVE)
        .first()
    )
    if not aggressive:
        return {"status": "ok", "deleted_rows": counts, "message": "no aggressive portfolio"}

    pid = aggressive.id
    for model in TRADING_TABLES:
        if hasattr(model, "portfolio_id"):
            n = (
                db.query(model)
                .filter(model.portfolio_id == pid)
                .delete(synchronize_session=False)
            )
            counts[model.__tablename__] = counts.get(model.__tablename__, 0) + n
        if hasattr(model, "profile"):
            n = (
                db.query(model)
                .filter(model.profile == PROFILE_AGGRESSIVE)
                .delete(synchronize_session=False)
            )
            counts[model.__tablename__] = counts.get(model.__tablename__, 0) + n

    db.delete(aggressive)
    counts["portfolios"] = 1
    db.commit()
    logger.info("Aggressive portfolio removed: %s", counts)
    return {"status": "ok", "deleted_rows": counts}
