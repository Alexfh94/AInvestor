"""Tests for exit rules and rotation helpers."""

from __future__ import annotations

from ainvestor.engine.exit_rules import (
    collect_price_stop_triggers,
    is_loss_stop_price,
    mandatory_close_proposals,
    normalize_perp_stop_loss,
    position_trend_aligned,
    roe_stop_loss_triggers,
    roe_take_profit_triggers,
    trend_reversal_close_proposal,
)
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
        leverage=20,
        margin_used=100.0,
        notional_usdt=2000.0,
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


def test_mandatory_close_on_profit_regardless_of_trend():
    pos = _perp_position(roe=25.0)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_1h="bullish", trend="bullish")
    proposals = mandatory_close_proposals(
        _snapshot([pos]),
        {"ETH/USDT": signal},
        {"ETH/USDT": 70},
        PROFILE_EXTREME,
    )
    assert len(proposals) == 1
    assert proposals[0].action.value == "sell"
    assert proposals[0].take_profit_pct == 0.0


def test_mandatory_close_on_loss_regardless_of_quant():
    """El corte a -16% ROE es incondicional, aunque el quant siga alto."""
    pos = _perp_position(roe=-17.0)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_1h="bearish", trend="bearish")
    proposals = mandatory_close_proposals(
        _snapshot([pos]),
        {"ETH/USDT": signal},
        {"ETH/USDT": 75},
        PROFILE_EXTREME,
    )
    assert len(proposals) == 1
    assert "corte de pérdida" in proposals[0].reasoning


def test_no_mandatory_close_on_moderate_loss_leaves_ai_decision():
    pos = _perp_position(roe=-3.0)
    signal = TechnicalSignal(symbol="ETH/USDT", trend_1h="bullish", trend="bullish")
    proposals = mandatory_close_proposals(
        _snapshot([pos]),
        {"ETH/USDT": signal},
        {"ETH/USDT": 35},
        PROFILE_EXTREME,
    )
    assert proposals == []


def test_roe_take_profit_triggers():
    pos = _perp_position(roe=25.0)
    triggers = roe_take_profit_triggers(_snapshot([pos]), PROFILE_EXTREME)
    assert triggers == [("ETH/USDT", 100.0)]


def test_roe_stop_loss_triggers():
    pos = _perp_position(roe=-17.0)
    triggers = roe_stop_loss_triggers(_snapshot([pos]), PROFILE_EXTREME)
    assert triggers == [("ETH/USDT", 100.0)]


def test_roe_stop_loss_no_trigger_on_moderate_loss():
    pos = _perp_position(roe=-3.0)
    assert roe_stop_loss_triggers(_snapshot([pos]), PROFILE_EXTREME) == []


def test_position_trend_aligned():
    sig = TechnicalSignal(symbol="X", trend_1h="bullish")
    assert position_trend_aligned("long", sig) is True
    assert position_trend_aligned("short", sig) is False


def test_loss_stop_only_below_entry_for_long():
    assert is_loss_stop_price(position_side="long", entry_price=100.0, stop_loss=99.5) is True
    assert is_loss_stop_price(position_side="long", entry_price=100.0, stop_loss=100.5) is False


def test_profit_locking_stop_not_triggered_for_perps():
    pos = _perp_position(roe=4.0)
    pos = pos.model_copy(
        update={"entry_price": 0.1903, "current_price": 0.1908, "stop_loss": 0.1908176}
    )
    triggers = collect_price_stop_triggers(_snapshot([pos]), PROFILE_EXTREME)
    assert triggers == []


def test_perp_loss_stop_normalized_from_profit_lock():
    from types import SimpleNamespace

    pos = SimpleNamespace(
        instrument_type="perpetual",
        entry_price=0.1905,
        position_side="long",
        leverage=20,
        stop_loss=0.1909174,
    )
    assert normalize_perp_stop_loss(pos, PROFILE_EXTREME) is True
    assert pos.stop_loss < pos.entry_price


def test_loss_stop_triggers_risk_sl_on_spot():
    pos = _perp_position(roe=-5.0)
    pos = pos.model_copy(
        update={
            "instrument_type": "spot",
            "entry_price": 100.0,
            "current_price": 99.0,
            "stop_loss": 99.2,
        }
    )
    triggers = collect_price_stop_triggers(_snapshot([pos]), PROFILE_EXTREME)
    assert len(triggers) == 1
    assert triggers[0][3] == "risk_sl"


def test_trend_reversal_requires_strong_opposite_signal():
    pos = _perp_position(roe=2.0, symbol="ADA/USDT")
    weak = TechnicalSignal(
        symbol="ADA/USDT",
        short_score=65,
        long_score=40,
        adx=25.0,
        trend_4h="bearish",
        trend_1h="neutral",
    )
    assert trend_reversal_close_proposal(pos, weak, PROFILE_EXTREME) is None

    strong = weak.model_copy(update={"short_score": 80, "long_score": 30})
    proposal = trend_reversal_close_proposal(pos, strong, PROFILE_EXTREME)
    assert proposal is not None
    assert proposal.action.value == "sell"
