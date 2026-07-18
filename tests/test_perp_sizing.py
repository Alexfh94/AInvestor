"""Tests for all-in perp sizing (float-safe margin + fee)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio, Position
from ainvestor.engine.executor import TradeExecutor
from ainvestor.models.schemas import DecisionAction, InstrumentType, RiskCheckResult, TradeProposal
from ainvestor.portfolio.perp_sizing import compute_all_in_perp_open
from ainvestor.portfolio.perp_simulator import PerpPaperSimulator
from ainvestor.portfolio.profiles import PROFILE_EXTREME


def test_all_in_sizing_uses_full_balance_without_overrun():
    balance = 106.02652253333372
    margin, notional, fee = compute_all_in_perp_open(balance, 10, 0.001, fee_reserve_pct=0.1)
    assert margin + fee <= balance
    assert margin + fee == pytest.approx(balance * 0.999, rel=1e-9)
    assert notional == pytest.approx(margin * 10, rel=1e-9)


@pytest.mark.asyncio
async def test_open_position_with_106_usdt_balance():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(
        Portfolio(
            mode="paper",
            profile=PROFILE_EXTREME,
            quote_balance=106.02652253333372,
            initial_balance=100.0,
            quote_currency="USDT",
        )
    )
    session.commit()

    portfolio = session.query(Portfolio).first()
    margin, notional, fee = compute_all_in_perp_open(
        portfolio.quote_balance, 10, 0.001
    )
    sim = PerpPaperSimulator(session, portfolio)
    trade = sim.open_position(
        "ETH/USDT",
        "long",
        notional,
        1880.0,
        10,
        1692.0,
        2160.0,
        margin_used=margin,
        opening_fee=fee,
    )
    assert trade is not None
    assert portfolio.quote_balance == pytest.approx(0.0, abs=1e-9)
    pos = session.query(Position).filter(Position.is_open == True).first()  # noqa: E712
    assert pos is not None
    assert pos.symbol == "ETH/USDT"
    session.close()


@pytest.mark.asyncio
async def test_executor_all_in_with_realistic_balance():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(
        Portfolio(
            mode="paper",
            profile=PROFILE_EXTREME,
            quote_balance=106.02652253333372,
            initial_balance=100.0,
            quote_currency="USDT",
        )
    )
    session.commit()

    executor = TradeExecutor(session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="ETH/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.2,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        position_side="long",
        leverage=10,
    )
    ok = await executor._execute_perp_paper(
        RiskCheckResult(approved=True, proposal=proposal).proposal,
        1880.0,
        "test-allin",
        0.0,
    )
    assert ok is True
    portfolio = session.query(Portfolio).first()
    assert portfolio.quote_balance < 0.2
    assert portfolio.quote_balance > 0
    session.close()
