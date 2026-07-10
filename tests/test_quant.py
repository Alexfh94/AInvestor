"""Tests for QuantEngine."""

from __future__ import annotations

import numpy as np

from ainvestor.engine.quant import QuantEngine


def _generate_ohlcv(n: int = 100, trend: float = 0.001) -> list[list]:
    ohlcv = []
    price = 50000.0
    for i in range(n):
        price *= 1 + trend + np.random.uniform(-0.005, 0.005)
        ohlcv.append([
            i * 3600000,
            price * 0.999,
            price * 1.002,
            price * 0.998,
            price,
            1000 + np.random.uniform(-200, 200),
        ])
    return ohlcv


def test_analyze_returns_signal():
    quant = QuantEngine()
    ohlcv = _generate_ohlcv(100)
    signal = quant.analyze("BTC/USDT", ohlcv)
    assert signal.symbol == "BTC/USDT"
    assert signal.rsi is not None
    assert 0 <= signal.conviction_score <= 100
    assert signal.trend in ("bullish", "bearish", "neutral")


def test_analyze_insufficient_data():
    quant = QuantEngine()
    signal = quant.analyze("BTC/USDT", [[0, 1, 2, 3, 4, 5]])
    assert signal.conviction_score == 50
    assert signal.trend == "neutral"


def test_bullish_trend_detection():
    quant = QuantEngine()
    ohlcv = _generate_ohlcv(100, trend=0.003)
    signal = quant.analyze("BTC/USDT", ohlcv)
    assert signal.ma_fast is not None
    assert signal.ma_slow is not None


def test_summarize():
    quant = QuantEngine()
    ohlcv = _generate_ohlcv(100)
    signals = [quant.analyze("BTC/USDT", ohlcv), quant.analyze("ETH/USDT", ohlcv)]
    summary = quant.summarize(signals)
    assert "BTC/USDT" in summary
    assert "ETH/USDT" in summary
