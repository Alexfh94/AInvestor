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
    quant = QuantEngine(profile="extreme")
    ohlcv = _generate_ohlcv(100)
    signal = quant.analyze("BTC/USDT", ohlcv)
    assert signal.symbol == "BTC/USDT"
    assert signal.rsi is not None
    assert 0 <= signal.long_score <= 100
    assert 0 <= signal.short_score <= 100
    assert signal.trend in ("bullish", "bearish", "neutral")
    assert signal.adx is not None


def test_analyze_insufficient_data():
    quant = QuantEngine(profile="extreme")
    signal = quant.analyze("BTC/USDT", [[0, 1, 2, 3, 4, 5]])
    assert signal.conviction_score == 50
    assert signal.trend == "neutral"


def test_bullish_trend_detection():
    quant = QuantEngine(profile="extreme")
    ohlcv = _generate_ohlcv(100, trend=0.003)
    signal = quant.analyze("BTC/USDT", ohlcv)
    assert signal.ema_fast is not None
    assert signal.ema_slow is not None


def test_directional_scoring():
    quant = QuantEngine(profile="extreme")
    ohlcv = _generate_ohlcv(100, trend=0.004)
    signal = quant.analyze("BTC/USDT", ohlcv)
    if signal.trend == "bullish":
        assert signal.long_score >= signal.short_score or signal.adx < quant.min_adx


def test_mtf_alignment():
    quant = QuantEngine(profile="extreme")
    ohlcv = _generate_ohlcv(100, trend=0.002)
    signal = quant.analyze_multi_timeframe(
        "BTC/USDT", {"1h": ohlcv, "4h": ohlcv, "1d": ohlcv}
    )
    trends = [signal.trend_1h, signal.trend_4h, signal.trend_1d]
    non_neutral = [t for t in trends if t and t != "neutral"]
    if len(non_neutral) >= 3 and all(t == non_neutral[0] for t in non_neutral):
        assert signal.mtf_aligned is True
    assert signal.trend_4h is not None
    assert signal.trend_1d is not None


def test_summarize():
    quant = QuantEngine(profile="extreme")
    ohlcv = _generate_ohlcv(100)
    signals = [quant.analyze("BTC/USDT", ohlcv), quant.analyze("ETH/USDT", ohlcv)]
    summary = quant.summarize(signals)
    assert "BTC/USDT" in summary
    assert "ETH/USDT" in summary
