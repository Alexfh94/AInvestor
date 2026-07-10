"""Tests for RiskManager."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio
from ainvestor.engine.risk import RiskManager
from ainvestor.models.schemas import (
    DecisionAction,
    PortfolioSnapshot,
    PositionSnapshot,
    TradeProposal,
    TradingMode,
)
from ainvestor.portfolio.profiles import PROFILE_AGGRESSIVE, PROFILE_CONSERVATIVE


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    portfolio = Portfolio(
        mode="paper",
        profile=PROFILE_CONSERVATIVE,
        quote_balance=10000.0,
        initial_balance=100.0,
        quote_currency="USDT",
    )
    session.add(portfolio)
    session.commit()
    yield session
    session.close()


def _snapshot(portfolio_id: int = 1, **kwargs) -> PortfolioSnapshot:
    defaults = {
        "mode": TradingMode.PAPER,
        "profile": PROFILE_CONSERVATIVE,
        "portfolio_id": portfolio_id,
        "quote_balance": 10000.0,
        "total_value_usdt": 10000.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "positions": [],
        "kill_switch_active": False,
    }
    defaults.update(kwargs)
    return PortfolioSnapshot(**defaults)


def test_approve_valid_buy(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BTC/USDT",
        amount_pct=5.0,
        stop_loss_pct=3.0,
        take_profit_pct=6.0,
        conviction=70,
    )
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio_id=portfolio.id), current_price=50000.0
    )
    assert result.approved is True


def test_reject_non_whitelist_symbol(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="SHIB/USDT",
        amount_pct=5.0,
        stop_loss_pct=3.0,
        take_profit_pct=6.0,
    )
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio_id=portfolio.id), current_price=0.00001
    )
    assert result.approved is False
    assert any("whitelist" in r.lower() for r in result.rejection_reasons)


def test_reject_kill_switch_active(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="ETH/USDT",
        amount_pct=5.0,
        stop_loss_pct=3.0,
        take_profit_pct=6.0,
    )
    result = risk.validate_proposal(
        proposal,
        _snapshot(portfolio_id=portfolio.id, kill_switch_active=True),
        current_price=3000.0,
    )
    assert result.approved is False
    assert "kill switch" in result.rejection_reasons[0].lower()


def test_reject_missing_stop_loss(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BTC/USDT",
        amount_pct=5.0,
        stop_loss_pct=0.0,
        take_profit_pct=6.0,
    )
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio_id=portfolio.id), current_price=50000.0
    )
    assert result.approved is False


def test_reject_oversized_position(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BTC/USDT",
        amount_pct=50.0,
        stop_loss_pct=3.0,
        take_profit_pct=6.0,
        conviction=50,
    )
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio_id=portfolio.id), current_price=50000.0
    )
    assert result.approved is False


def test_approve_high_conviction_large_position(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BTC/USDT",
        amount_pct=45.0,
        stop_loss_pct=3.0,
        take_profit_pct=8.0,
        conviction=90,
    )
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio_id=portfolio.id), current_price=50000.0
    )
    assert result.approved is True


def test_conviction_scaling(db_session):
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    low = risk.max_position_pct_for_conviction(40)
    high = risk.max_position_pct_for_conviction(95)
    assert low < high
    assert high <= 60.0


def test_approve_hold(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    proposal = TradeProposal(
        action=DecisionAction.HOLD,
        symbol="BTC/USDT",
        amount_pct=0,
        stop_loss_pct=0,
        take_profit_pct=0,
    )
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio_id=portfolio.id), current_price=50000.0
    )
    assert result.approved is True


def test_stop_loss_trigger(db_session):
    risk = RiskManager(db_session, profile=PROFILE_CONSERVATIVE)
    snapshot = _snapshot(
        positions=[
            PositionSnapshot(
                symbol="BTC/USDT",
                asset="BTC",
                amount=0.1,
                entry_price=50000,
                current_price=48000,
                value_usdt=4800,
                pct_of_portfolio=48.0,
                unrealized_pnl=-200,
                stop_loss=48500,
                take_profit=55000,
            )
        ]
    )
    triggers = risk.check_stop_loss_take_profit(snapshot)
    assert len(triggers) == 1
    assert triggers[0][0] == "BTC/USDT"
    assert triggers[0][1] == "sell"
