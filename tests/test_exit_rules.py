"""Tests for exit rules and rotation helpers."""

from __future__ import annotations

from ainvestor.engine.exit_rules import mandatory_close_proposals, position_trend_aligned
from ainvestor.models.schemas import (
    PortfolioSnapshot,
    PositionSnapshot,
    TechnicalSignal,
    TradingMode,
)
from ainvestor.portfolio.profiles import PROFILE_EXTREME


def _perp_position(roe: float, symbol: str = "ETH/USDT", side: str = "long") -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset=symbol.split("/")[0],
        amount=1.0,
        entry_price=100.0,
        current_price=100.0,
        value_usdt=100.0,
        pct_of_portfolio=100.0,
        unrealized_pnl=roe,
        instrument_type="perpetual",
        position_side=side,
        leverage=10,
        margin_used=100.0,
        notional_usdt=1000.0,
        roe_pct=roe,
    )


def _snapshot(positions: list[PositionSnapshot]) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        mode=TradingMode.PAPER,
        profile=PROFILE_EXTREME,
        portfolio_id=1,
        quote_balance=0.0,
        total_value_usdt=100.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        positions=positions,
    )


def test_mandatory_close_on_profit_with_misaligned_trend():
    pos = _perp_position(roe=15.0)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_1h="bearish", trend="bearish")
    proposals = mandatory_close_proposals(
        _snapshot([pos]),
        {"ETH/USDT": signal},
        {"ETH/USDT": 50},
        PROFILE_EXTREME,
    )
    assert len(proposals) == 1
    assert proposals[0].action.value == "sell"


def test_mandatory_close_on_loss_low_quant():
    pos = _perp_position(roe=-6.0)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_1h="bearish", trend="bearish")
    proposals = mandatory_close_proposals(
        _snapshot([pos]),
        {"ETH/USDT": signal},
        {"ETH/USDT": 35},
        PROFILE_EXTREME,
    )
    assert len(proposals) == 1


def test_no_mandatory_close_when_trend_aligned():
    pos = _perp_position(roe=15.0)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_1h="bullish", trend="bullish")
    proposals = mandatory_close_proposals(
        _snapshot([pos]),
        {"ETH/USDT": signal},
        {"ETH/USDT": 70},
        PROFILE_EXTREME,
    )
    assert proposals == []


def test_position_trend_aligned():
    sig = TechnicalSignal(symbol="X", trend_1h="bullish")
    assert position_trend_aligned("long", sig) is True
    assert position_trend_aligned("short", sig) is False
