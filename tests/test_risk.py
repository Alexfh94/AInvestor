"""Tests for RiskManager."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ainvestor.db.models import Base, Portfolio
from ainvestor.engine.risk import RiskManager
from ainvestor.models.schemas import (
    DecisionAction,
    InstrumentType,
    PortfolioSnapshot,
    PositionSnapshot,
    TradeProposal,
    TradingMode,
)
from ainvestor.portfolio.profiles import PROFILE_EXTREME


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    portfolio = Portfolio(
        mode="paper",
        profile=PROFILE_EXTREME,
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
        "profile": PROFILE_EXTREME,
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
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BTC/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.2,
        conviction=70,
        instrument_type=InstrumentType.PERPETUAL,
        leverage=10,
        position_side="long",
    )
    result = risk.validate_proposal(
        proposal,
        _snapshot(portfolio_id=portfolio.id),
        current_price=50000.0,
        derivatives_available=True,
    )
    assert result.approved is True


def test_reject_non_whitelist_symbol(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
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
    assert any(
        "whitelist" in r.lower() or "perpetual" in r.lower()
        for r in result.rejection_reasons
    )


def test_reject_kill_switch_active(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
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
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
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
    from ainvestor.models.schemas import InstrumentType

    portfolio = Portfolio(
        mode="paper",
        profile=PROFILE_EXTREME,
        quote_balance=10000.0,
        initial_balance=100.0,
        quote_currency="USDT",
    )
    db_session.add(portfolio)
    db_session.commit()

    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="SOL/USDT",
        amount_pct=50.0,
        stop_loss_pct=10.0,
        take_profit_pct=6.0,
        conviction=50,
        instrument_type=InstrumentType.PERPETUAL,
        leverage=10,
        position_side="long",
    )
    result = risk.validate_proposal(
        proposal,
        PortfolioSnapshot(
            mode=TradingMode.PAPER,
            profile=PROFILE_EXTREME,
            portfolio_id=portfolio.id,
            quote_balance=10000.0,
            total_value_usdt=10000.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            positions=[],
            kill_switch_active=False,
        ),
        current_price=150.0,
        derivatives_available=True,
    )
    assert result.approved is False
    assert any("all-in" in r.lower() for r in result.rejection_reasons)


def test_approve_high_conviction_large_position(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BTC/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.2,
        conviction=90,
        instrument_type=InstrumentType.PERPETUAL,
        leverage=10,
        position_side="long",
    )
    result = risk.validate_proposal(
        proposal,
        _snapshot(portfolio_id=portfolio.id),
        current_price=50000.0,
        derivatives_available=True,
    )
    assert result.approved is True


def test_conviction_scaling(db_session):
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    low = risk.max_position_pct_for_conviction(40)
    high = risk.max_position_pct_for_conviction(95)
    assert low == 100.0
    assert high == 100.0


def test_approve_hold(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
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
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
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


def test_perp_accepts_target_tp_after_fees(db_session):
    """Perp TP target 1.2% passes validation; sub-minimum is auto-adjusted."""
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BNB/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.2,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        leverage=10,
        position_side="long",
    )
    result = risk.validate_proposal(
        proposal,
        _snapshot(portfolio_id=portfolio.id),
        current_price=600.0,
        fee_rate=0.001,
        derivatives_available=True,
    )
    assert result.approved is True


def test_perp_auto_adjusts_low_tp_to_target(db_session):
    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    proposal = TradeProposal(
        action=DecisionAction.BUY,
        symbol="BNB/USDT",
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=0.5,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        leverage=10,
        position_side="long",
    )
    result = risk.validate_proposal(
        proposal,
        _snapshot(portfolio_id=portfolio.id),
        current_price=600.0,
        fee_rate=0.001,
        derivatives_available=True,
    )
    assert result.approved is True
    assert result.proposal.take_profit_pct == 1.2


def _open_proposal(symbol: str = "ETH/USDT", side: str = "long") -> TradeProposal:
    action = DecisionAction.BUY if side == "long" else DecisionAction.SELL
    return TradeProposal(
        action=action,
        symbol=symbol,
        amount_pct=100.0,
        stop_loss_pct=10.0,
        take_profit_pct=1.2,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        leverage=10,
        position_side=side,
    )


def test_perp_open_blocked_against_4h_trend(db_session):
    from ainvestor.models.schemas import TechnicalSignal

    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_4h="bearish")
    result = risk.validate_proposal(
        _open_proposal(side="long"),
        _snapshot(portfolio_id=portfolio.id),
        current_price=3000.0,
        derivatives_available=True,
        signal=signal,
    )
    assert result.approved is False
    assert any("4h trend" in r for r in result.rejection_reasons)


def test_perp_open_allowed_with_4h_trend_aligned(db_session):
    from ainvestor.models.schemas import TechnicalSignal

    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_4h="bullish", atr_pct=0.6)
    result = risk.validate_proposal(
        _open_proposal(side="long"),
        _snapshot(portfolio_id=portfolio.id),
        current_price=3000.0,
        derivatives_available=True,
        signal=signal,
    )
    assert result.approved is True


def test_perp_open_blocked_when_atr_too_low_for_tp(db_session):
    from ainvestor.models.schemas import TechnicalSignal

    portfolio = db_session.query(Portfolio).first()
    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_4h="bullish", atr_pct=0.2)
    result = risk.validate_proposal(
        _open_proposal(side="long"),
        _snapshot(portfolio_id=portfolio.id),
        current_price=3000.0,
        derivatives_available=True,
        signal=signal,
    )
    assert result.approved is False
    assert any("ATR" in r for r in result.rejection_reasons)


def test_perp_close_blocked_by_min_hold_in_roe_band(db_session):
    from ainvestor.db.models import Position

    portfolio = db_session.query(Portfolio).first()
    db_session.add(
        Position(
            portfolio_id=portfolio.id,
            symbol="ETH/USDT",
            amount=1.0,
            entry_price=3000.0,
            instrument_type="perpetual",
            position_side="long",
            leverage=10,
            margin_used=100.0,
            is_open=True,
        )
    )
    db_session.commit()

    risk = RiskManager(db_session, profile=PROFILE_EXTREME)
    close_proposal = TradeProposal(
        action=DecisionAction.SELL,
        symbol="ETH/USDT",
        amount_pct=100.0,
        stop_loss_pct=0.8,
        take_profit_pct=1.2,
        conviction=80,
        instrument_type=InstrumentType.PERPETUAL,
        leverage=10,
        position_side="long",
    )
    snapshot = _snapshot(
        portfolio_id=portfolio.id,
        positions=[
            PositionSnapshot(
                symbol="ETH/USDT",
                asset="ETH",
                amount=1.0,
                entry_price=3000.0,
                current_price=3003.0,
                value_usdt=100.0,
                pct_of_portfolio=100.0,
                unrealized_pnl=1.0,
                instrument_type="perpetual",
                position_side="long",
                leverage=10,
                margin_used=100.0,
                notional_usdt=1000.0,
                roe_pct=1.0,
            )
        ],
    )
    result = risk.validate_proposal(
        close_proposal, snapshot, current_price=3003.0, derivatives_available=True
    )
    assert result.approved is False
    assert any("Min hold" in r for r in result.rejection_reasons)
