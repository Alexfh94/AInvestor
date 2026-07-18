"""Tests for perpetual economics: margin×leverage, snapshot PnL, shorts."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio, Position
from ainvestor.engine.executor import TradeExecutor
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
            quote_balance=1000.0,
            initial_balance=1000.0,
            quote_currency="USDT",
        )
    )
    session.commit()
    yield session
    session.close()


@pytest.mark.asyncio
async def test_perp_long_snapshot_pnl_and_roe(db_session):
    portfolio = db_session.query(Portfolio).first()
    sim = PerpPaperSimulator(db_session, portfolio)
    # 10% margin = 100, 10x => notional 1000 at price 100
    trade = sim.open_position("SOL/USDT", "long", 1000.0, 100.0, 10, 95.0, 110.0)
    assert trade is not None
    assert portfolio.quote_balance == pytest.approx(899.0, rel=1e-3)  # 100 margin + 1 fee

    mgr = PortfolioManager(db_session, profile=PROFILE_EXTREME)
    snap = await mgr.get_snapshot({"SOL/USDT": 105.0})
    pos = snap.positions[0]
    assert pos.instrument_type == "perpetual"
    assert pos.leverage == 10
    assert pos.margin_used == pytest.approx(100.0, rel=1e-2)
    assert pos.unrealized_pnl == pytest.approx(50.0, rel=1e-2)  # (105-100)*10 SOL
    assert pos.roe_pct == pytest.approx(50.0, rel=1e-1)
    assert snap.total_value_usdt == pytest.approx(899 + 100 + 50, rel=1e-2)


@pytest.mark.asyncio
async def test_perp_short_snapshot_pnl(db_session):
    portfolio = db_session.query(Portfolio).first()
    sim = PerpPaperSimulator(db_session, portfolio)
    trade = sim.open_position("ETH/USDT", "short", 500.0, 2000.0, 5, 2100.0, 1900.0)
    assert trade is not None

    mgr = PortfolioManager(db_session, profile=PROFILE_EXTREME)
    snap = await mgr.get_snapshot({"ETH/USDT": 1950.0})
    pos = snap.positions[0]
    assert pos.position_side == "short"
    assert pos.unrealized_pnl == pytest.approx(12.5, rel=1e-2)  # (2000-1950)*0.25 ETH


@pytest.mark.asyncio
async def test_executor_perp_margin_times_leverage(db_session):
    portfolio = db_session.query(Portfolio).first()
    executor = TradeExecutor(db_session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="SOL/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=10.0,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )
    from ainvestor.models.schemas import RiskCheckResult

    result = RiskCheckResult(approved=True, proposal=proposal)
    ok = await executor._execute_perp_paper(result.proposal, 100.0, "test-cycle", 0.0)
    assert ok is True
    pos = db_session.query(Position).filter(Position.is_open == True).first()  # noqa: E712
    assert pos is not None
    assert pos.leverage == 10
    expected_margin = 1000.0 / (1 + 10 * 0.001)
    assert pos.margin_used == pytest.approx(expected_margin, rel=1e-2)


def test_risk_perp_validates_margin_not_notional(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    snap = PortfolioSnapshot(
        mode=TradingMode.PAPER,
        profile=PROFILE_EXTREME,
        portfolio_id=portfolio.id,
        quote_balance=1000.0,
        total_value_usdt=1000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        positions=[],
        kill_switch_active=False,
    )
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="SOL/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.5,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )
    result = risk.validate_proposal(
        proposal, snap, 100.0, funding_rate=0.0001, derivatives_available=True
    )
    assert result.approved is True


def test_stop_loss_inverted_for_short(db_session):
    from ainvestor.models.schemas import PositionSnapshot

    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    snap = PortfolioSnapshot(
        mode=TradingMode.PAPER,
        profile=PROFILE_EXTREME,
        portfolio_id=portfolio.id,
        quote_balance=500.0,
        total_value_usdt=1000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        positions=[
            PositionSnapshot(
                symbol="ETH/USDT",
                asset="ETH",
                amount=1.0,
                entry_price=2000.0,
                current_price=2050.0,
                value_usdt=100.0,
                pct_of_portfolio=10.0,
                unrealized_pnl=-50.0,
                stop_loss=2040.0,
                take_profit=1900.0,
                instrument_type="perpetual",
                position_side="short",
                leverage=5,
            )
        ],
        kill_switch_active=False,
    )
    triggers = risk.check_stop_loss_take_profit(snap)
    assert any(t[0] == "ETH/USDT" for t in triggers)
