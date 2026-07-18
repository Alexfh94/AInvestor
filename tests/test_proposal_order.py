"""Tests for close-before-open proposal ordering in a cycle."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio, Position
from ainvestor.engine.executor import TradeExecutor
from ainvestor.engine.proposal_order import is_close_proposal, sort_proposals_for_execution
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


def _snapshot_with_link_long(db_session) -> PortfolioSnapshot:
    import asyncio

    portfolio = db_session.query(Portfolio).first()
    sim = PerpPaperSimulator(db_session, portfolio)
    sim.open_position("LINK/USDT", "long", 900.0, 10.0, 10, 9.0, 11.0)
    mgr = PortfolioManager(db_session, profile=PROFILE_EXTREME)

    return asyncio.run(mgr.get_snapshot({"LINK/USDT": 10.0, "ETH/USDT": 2000.0}))


def test_sort_puts_close_before_open(db_session):
    snap = _snapshot_with_link_long(db_session)
    open_eth = TradeProposal(
        action=DecisionAction.BUY,
        symbol="ETH/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.5,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )
    close_link = TradeProposal(
        action=DecisionAction.SELL,
        symbol="LINK/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.5,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )

    ordered = sort_proposals_for_execution([open_eth, close_link], snap)
    assert ordered[0].symbol == "LINK/USDT"
    assert ordered[1].symbol == "ETH/USDT"
    assert is_close_proposal(close_link, snap)
    assert not is_close_proposal(open_eth, snap)


def test_flip_eth_long_to_short_orders_close_before_open(db_session):
    import asyncio

    portfolio = db_session.query(Portfolio).first()
    sim = PerpPaperSimulator(db_session, portfolio)
    sim.open_position("ETH/USDT", "long", 900.0, 2000.0, 10, 1800.0, 2200.0)
    mgr = PortfolioManager(db_session, profile=PROFILE_EXTREME)

    snap = asyncio.run(mgr.get_snapshot({"ETH/USDT": 2000.0}))

    open_short = TradeProposal(
        action=DecisionAction.SELL,
        symbol="ETH/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.5,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="short",
        leverage=10,
    )
    close_long = TradeProposal(
        action=DecisionAction.SELL,
        symbol="ETH/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.5,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )

    ordered = sort_proposals_for_execution([open_short, close_long], snap)
    assert ordered[0].position_side == "long"
    assert ordered[1].position_side == "short"


@pytest.mark.asyncio
async def test_rotation_executes_close_then_open(db_session):
    portfolio = db_session.query(Portfolio).first()
    sim = PerpPaperSimulator(db_session, portfolio)
    sim.open_position("LINK/USDT", "long", 900.0, 10.0, 10, 9.0, 11.0)

    mgr = PortfolioManager(db_session, profile=PROFILE_EXTREME)
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    executor = TradeExecutor(db_session, profile=PROFILE_EXTREME)

    prices = {"LINK/USDT": 10.5, "ETH/USDT": 2000.0}
    snapshot = await mgr.get_snapshot(prices)

    close_link = TradeProposal(
        action=DecisionAction.SELL,
        symbol="LINK/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.5,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )
    open_eth = TradeProposal(
        action=DecisionAction.BUY,
        symbol="ETH/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.5,
        conviction=85,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )

    ordered = sort_proposals_for_execution([open_eth, close_link], snapshot)
    assert ordered[0].symbol == "LINK/USDT"

    for proposal in ordered:
        price = prices[proposal.symbol]
        check = risk.validate_proposal(
            proposal,
            snapshot,
            price,
            fee_rate=0.001,
            derivatives_available=True,
            cycle_proposals=ordered,
        )
        assert check.approved, check.rejection_reasons
        ok = await executor.execute_approved(check, price, "test-rotation")
        assert ok
        snapshot = await mgr.get_snapshot(prices)

    positions = db_session.query(Position).filter(Position.is_open == True).all()  # noqa: E712
    assert len(positions) == 1
    assert positions[0].symbol == "ETH/USDT"
    assert positions[0].position_side == "long"
