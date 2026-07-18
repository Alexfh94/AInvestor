"""Tests for extreme-only all-in perpetual profile."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio, Trade
from ainvestor.engine.risk import RiskManager
from ainvestor.models.schemas import (
    DecisionAction,
    InstrumentType,
    PortfolioSnapshot,
    TradeProposal,
    TradingMode,
)
from ainvestor.portfolio.manager import PortfolioManager
from ainvestor.portfolio.perp_simulator import PerpPaperSimulator
from ainvestor.portfolio.profiles import PROFILE_EXTREME
from ainvestor.utils.datetime_utils import app_now


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(
        Portfolio(
            mode="paper",
            profile=PROFILE_EXTREME,
            quote_balance=100.0,
            initial_balance=100.0,
            quote_currency="USDT",
        )
    )
    session.commit()
    yield session
    session.close()


def _perp_proposal(
    *,
    symbol: str = "BTC/USDT",
    action: DecisionAction = DecisionAction.BUY,
    position_side: str = "long",
    amount_pct: float = 100.0,
    stop_loss_pct: float = 10.0,
    take_profit_pct: float = 1.5,
    leverage: int = 10,
) -> TradeProposal:
    return TradeProposal(
        action=action,
        symbol=symbol,
        amount_pct=amount_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side=position_side,
        leverage=leverage,
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


def test_extreme_rejects_spot(db_session):
    portfolio = db_session.query(Portfolio).filter(Portfolio.profile == PROFILE_EXTREME).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BTC/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=5.0,
        conviction=80,
    )
    result = risk.validate_proposal(proposal, _snapshot(portfolio), current_price=50000.0)
    assert result.approved is False
    assert any("perpetual" in r.lower() for r in result.rejection_reasons)


def test_extreme_requires_all_in(db_session):
    portfolio = db_session.query(Portfolio).filter(Portfolio.profile == PROFILE_EXTREME).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = _perp_proposal(amount_pct=50.0)
    result = risk.validate_proposal(proposal, _snapshot(portfolio), current_price=50000.0)
    assert result.approved is False
    assert any("all-in" in r.lower() for r in result.rejection_reasons)


def test_extreme_rejects_stop_loss_below_leverage_floor(db_session):
    portfolio = db_session.query(Portfolio).filter(Portfolio.profile == PROFILE_EXTREME).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = _perp_proposal(stop_loss_pct=5.0, leverage=10)
    result = risk.validate_proposal(proposal, _snapshot(portfolio), current_price=50000.0)
    assert result.approved is False
    assert any("stop-loss" in r.lower() for r in result.rejection_reasons)


def test_extreme_accepts_all_in_perp(db_session):
    portfolio = db_session.query(Portfolio).filter(Portfolio.profile == PROFILE_EXTREME).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = _perp_proposal()
    result = risk.validate_proposal(
        proposal, _snapshot(portfolio), current_price=50000.0, derivatives_available=True
    )
    assert result.approved is True


def test_perp_close_records_pnl_on_trade(db_session):
    portfolio = db_session.query(Portfolio).filter(Portfolio.profile == PROFILE_EXTREME).first()
    sim = PerpPaperSimulator(db_session, portfolio)
    open_trade = sim.open_position(
        "ETH/USDT", "long", notional_usdt=990.0, price=2000.0, leverage=10,
        stop_loss=1800.0, take_profit=2200.0, fee_rate=0.001,
    )
    assert open_trade is not None
    assert open_trade.trade_action == "open"
    pos = db_session.query(Portfolio).first()
    from ainvestor.db.models import Position
    position = db_session.query(Position).filter(Position.is_open == True).first()
    close_trade = sim.close_position(position, price=2100.0, close_pct=100.0, fee_rate=0.001)
    assert close_trade is not None
    assert close_trade.trade_action == "close"
    assert close_trade.realized_pnl_usdt is not None
    assert close_trade.pnl_pct_roe is not None


def test_profile_ai_cycle_interval_extreme():
    from ainvestor.config import get_profile_ai_cycle_interval

    assert get_profile_ai_cycle_interval(PROFILE_EXTREME) == 15


def test_normalize_legacy_aggressive_maps_to_extreme():
    from ainvestor.portfolio.profiles import normalize_profile

    assert normalize_profile("aggressive") == PROFILE_EXTREME


def test_portfolio_manager_single_extreme(db_session):
    mgr = PortfolioManager(db_session, profile=PROFILE_EXTREME)
    port = mgr.get_or_create_portfolio()
    assert port.profile == PROFILE_EXTREME
    assert mgr.get_initial_value() == 100.0
