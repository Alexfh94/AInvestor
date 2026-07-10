"""Tests for dual portfolio (conservative vs aggressive)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio, Trade
from ainvestor.engine.risk import RiskManager
from ainvestor.models.schemas import (
    DecisionAction,
    PortfolioSnapshot,
    TradeProposal,
    TradingMode,
)
from ainvestor.portfolio.manager import PortfolioManager
from ainvestor.portfolio.profiles import PROFILE_AGGRESSIVE, PROFILE_CONSERVATIVE
from ainvestor.utils.datetime_utils import app_now


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    conservative = Portfolio(
        mode="paper",
        profile=PROFILE_CONSERVATIVE,
        quote_balance=100.0,
        initial_balance=100.0,
        quote_currency="USDT",
    )
    aggressive = Portfolio(
        mode="paper",
        profile=PROFILE_AGGRESSIVE,
        quote_balance=100.0,
        initial_balance=100.0,
        quote_currency="USDT",
    )
    session.add_all([conservative, aggressive])
    session.commit()
    yield session
    session.close()


def _proposal(symbol: str) -> TradeProposal:
    return TradeProposal(
        action=DecisionAction.BUY,
        symbol=symbol,
        amount_pct=5.0,
        stop_loss_pct=3.0,
        take_profit_pct=6.0,
        conviction=70,
    )


def _snapshot(portfolio: Portfolio) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        mode=TradingMode.PAPER,
        profile=portfolio.profile,
        portfolio_id=portfolio.id,
        quote_balance=portfolio.quote_balance,
        total_value_usdt=portfolio.quote_balance,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        positions=[],
        kill_switch_active=False,
    )


def test_conservative_accepts_btc(db_session):
    portfolio = (
        db_session.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_CONSERVATIVE)
        .first()
    )
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    result = risk.validate_proposal(
        _proposal("BTC/USDT"), _snapshot(portfolio), current_price=50000.0
    )
    assert result.approved is True


def test_aggressive_rejects_btc_and_eth(db_session):
    portfolio = (
        db_session.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_AGGRESSIVE)
        .first()
    )
    risk = RiskManager(db_session, profile=PROFILE_AGGRESSIVE)
    for symbol in ("BTC/USDT", "ETH/USDT"):
        result = risk.validate_proposal(
            _proposal(symbol), _snapshot(portfolio), current_price=1000.0
        )
        assert result.approved is False
        assert any("whitelist" in r.lower() for r in result.rejection_reasons)


def test_aggressive_accepts_alt(db_session):
    portfolio = (
        db_session.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_AGGRESSIVE)
        .first()
    )
    risk = RiskManager(db_session, profile=PROFILE_AGGRESSIVE)
    result = risk.validate_proposal(
        _proposal("SOL/USDT"), _snapshot(portfolio), current_price=150.0
    )
    assert result.approved is True


def test_trade_limits_isolated_between_portfolios(db_session):
    conservative = (
        db_session.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_CONSERVATIVE)
        .first()
    )
    aggressive = (
        db_session.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_AGGRESSIVE)
        .first()
    )
    now = app_now()
    for _ in range(6):
        db_session.add(
            Trade(
                portfolio_id=conservative.id,
                symbol="SOL/USDT",
                side="buy",
                amount=1.0,
                price=100.0,
                value_usdt=100.0,
                fee=0.1,
                mode="paper",
                status="executed",
                executed_at=now,
            )
        )
    db_session.commit()

    cons_risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    agg_risk = RiskManager(db_session, profile=PROFILE_AGGRESSIVE)

    cons_result = cons_risk.validate_proposal(
        _proposal("BTC/USDT"), _snapshot(conservative), current_price=50000.0
    )
    agg_result = agg_risk.validate_proposal(
        _proposal("SOL/USDT"), _snapshot(aggressive), current_price=150.0
    )

    assert cons_result.approved is False
    assert any("trade" in r.lower() or "daily" in r.lower() for r in cons_result.rejection_reasons)
    assert agg_result.approved is True


def test_aggressive_allows_all_in_high_conviction(db_session):
    portfolio = (
        db_session.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_AGGRESSIVE)
        .first()
    )
    risk = RiskManager(db_session, profile=PROFILE_AGGRESSIVE)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="DOGE/USDT",
        amount_pct=95.0,
        stop_loss_pct=4.0,
        take_profit_pct=12.0,
        conviction=85,
    )
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio), current_price=0.2
    )
    assert result.approved is True


def test_aggressive_perp_simulator_uses_profile_leverage(db_session):
    from ainvestor.portfolio.perp_simulator import PerpPaperSimulator

    portfolio = (
        db_session.query(Portfolio)
        .filter(Portfolio.profile == PROFILE_AGGRESSIVE)
        .first()
    )
    sim = PerpPaperSimulator(db_session, portfolio)
    assert int(sim.config.get("max_leverage", 1)) == 2


def test_portfolio_manager_resolves_distinct_portfolios(db_session):
    mgr_c = PortfolioManager(db_session, profile=PROFILE_CONSERVATIVE)
    mgr_a = PortfolioManager(db_session, profile=PROFILE_AGGRESSIVE)

    port_c = mgr_c.get_or_create_portfolio()
    port_a = mgr_a.get_or_create_portfolio()

    assert port_c.id != port_a.id
    assert port_c.profile == PROFILE_CONSERVATIVE
    assert port_a.profile == PROFILE_AGGRESSIVE
    assert mgr_c.get_initial_value() == 100.0
    assert mgr_a.get_initial_value() == 100.0
