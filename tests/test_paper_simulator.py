"""Tests for PaperTradingSimulator."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio, Trade
from ainvestor.portfolio.manager import PaperTradingSimulator, PortfolioManager


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    portfolio = Portfolio(mode="paper", quote_balance=10000.0, quote_currency="USDT")
    session.add(portfolio)
    session.commit()
    yield session
    session.close()


def test_buy_reduces_balance(db_session):
    portfolio = db_session.query(Portfolio).first()
    sim = PaperTradingSimulator(db_session, portfolio)
    initial = portfolio.quote_balance

    trade = sim.execute_buy("BTC/USDT", 1000.0, 50000.0, stop_loss=48000, take_profit=55000)
    assert trade is not None
    assert portfolio.quote_balance < initial
    assert len(sim.get_open_positions()) == 1


def test_sell_realizes_pnl(db_session):
    portfolio = db_session.query(Portfolio).first()
    sim = PaperTradingSimulator(db_session, portfolio)
    sim.execute_buy("ETH/USDT", 1000.0, 3000.0)

    trade = sim.execute_sell("ETH/USDT", 1000.0 / 3000.0, 3300.0)
    assert trade is not None
    assert portfolio.realized_pnl > 0
    assert len(sim.get_open_positions()) == 0


def test_insufficient_balance_rejected(db_session):
    portfolio = db_session.query(Portfolio).first()
    sim = PaperTradingSimulator(db_session, portfolio)
    trade = sim.execute_buy("BTC/USDT", 50000.0, 50000.0)
    assert trade is None


@pytest.mark.asyncio
async def test_portfolio_snapshot(db_session):
    portfolio = db_session.query(Portfolio).first()
    sim = PaperTradingSimulator(db_session, portfolio)
    sim.execute_buy("BTC/USDT", 1000.0, 50000.0)
    mgr = PortfolioManager(db_session)

    snapshot = await mgr.get_snapshot({"BTC/USDT": 52000.0})
    assert snapshot.total_value_usdt > 10000.0
    assert len(snapshot.positions) == 1
    assert snapshot.unrealized_pnl > 0
