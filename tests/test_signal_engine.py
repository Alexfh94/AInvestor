"""Tests for SignalEngine."""

from __future__ import annotations

import numpy as np

from ainvestor.engine.quant import QuantEngine
from ainvestor.engine.signal_engine import SignalEngine
from ainvestor.models.schemas import (
    DerivativesSnapshot,
    PortfolioSnapshot,
    TradingMode,
)
from ainvestor.utils.datetime_utils import app_now


def _generate_ohlcv(n: int = 100, trend: float = 0.003) -> list[list]:
    ohlcv = []
    price = 50000.0
    for i in range(n):
        price *= 1 + trend + np.random.uniform(-0.002, 0.002)
        ohlcv.append([
            i * 3600000,
            price * 0.999,
            price * 1.003,
            price * 0.998,
            price,
            5000 + np.random.uniform(0, 2000),
        ])
    return ohlcv


def _deriv(symbol: str, price: float) -> DerivativesSnapshot:
    return DerivativesSnapshot(
        symbol=symbol,
        funding_rate=-0.0001,
        funding_rate_pct=-0.01,
        mark_price=price,
        open_interest=1_000_000.0,
        timestamp=app_now(),
    )


def _empty_snapshot(balance: float = 100.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        mode=TradingMode.PAPER,
        profile="extreme",
        quote_balance=balance,
        total_value_usdt=balance,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        positions=[],
    )


def test_signal_engine_hold_when_no_strong_setup():
    quant = QuantEngine(profile="extreme")
    engine = SignalEngine(profile="extreme")
    ohlcv = _generate_ohlcv(30, trend=0.0)
    signal = quant.analyze("BTC/USDT", ohlcv)
    evaluation = engine.evaluate(
        [signal],
        {"BTC/USDT": _deriv("BTC/USDT", ohlcv[-1][4])},
        _empty_snapshot(),
    )
    assert evaluation.decision.hold is True
    assert evaluation.decision.proposals == []


def test_signal_engine_selects_best_setup():
    quant = QuantEngine(profile="extreme")
    engine = SignalEngine(profile="extreme")
    ohlcv = _generate_ohlcv(100, trend=0.004)
    mtf = {"1h": ohlcv, "4h": ohlcv, "1d": ohlcv}
    signal = quant.analyze_multi_timeframe("BTC/USDT", mtf)
    price = ohlcv[-1][4]
    evaluation = engine.evaluate(
        [signal],
        {"BTC/USDT": _deriv("BTC/USDT", price)},
        _empty_snapshot(),
    )
    if signal.tradable and max(signal.long_score, signal.short_score) >= 60:
        assert evaluation.decision.hold is False
        assert len(evaluation.decision.proposals) == 1
        p = evaluation.decision.proposals[0]
        assert p.leverage == 20
        assert p.amount_pct == 100.0
        assert p.take_profit_pct == 0.0


def test_signal_engine_hold_with_open_position():
    from ainvestor.models.schemas import PositionSnapshot

    engine = SignalEngine(profile="extreme")
    quant = QuantEngine(profile="extreme")
    ohlcv = _generate_ohlcv(100, trend=0.004)
    signal = quant.analyze_multi_timeframe("BTC/USDT", {"1h": ohlcv, "4h": ohlcv, "1d": ohlcv})
    snapshot = PortfolioSnapshot(
        mode=TradingMode.PAPER,
        profile="extreme",
        quote_balance=0.0,
        total_value_usdt=100.0,
        unrealized_pnl=5.0,
        realized_pnl=0.0,
        positions=[
            PositionSnapshot(
                symbol="BTC/USDT",
                asset="BTC",
                amount=0.01,
                entry_price=50000.0,
                current_price=51000.0,
                value_usdt=100.0,
                pct_of_portfolio=100.0,
                unrealized_pnl=5.0,
                instrument_type="perpetual",
                position_side="long",
                leverage=20,
                margin_used=100.0,
                roe_pct=5.0,
            )
        ],
    )
    evaluation = engine.evaluate(
        [signal],
        {"BTC/USDT": _deriv("BTC/USDT", ohlcv[-1][4])},
        snapshot,
    )
    assert evaluation.decision.hold is True
    assert evaluation.decision.proposals == []
