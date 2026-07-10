from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from ainvestor.models.schemas import TechnicalSignal

logger = logging.getLogger(__name__)


class QuantEngine:
    """Technical analysis signals: RSI, MA crossover, MACD, volume."""

    def __init__(self, rsi_period: int = 14, ma_fast: int = 9, ma_slow: int = 21):
        self.rsi_period = rsi_period
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow

    def analyze(self, symbol: str, ohlcv: list[list]) -> TechnicalSignal:
        if len(ohlcv) < self.ma_slow + 5:
            return TechnicalSignal(symbol=symbol, conviction_score=50, trend="neutral")

        df = self._to_dataframe(ohlcv)
        rsi = self._calc_rsi(df["close"])
        ma_fast = df["close"].rolling(self.ma_fast).mean().iloc[-1]
        ma_slow = df["close"].rolling(self.ma_slow).mean().iloc[-1]
        macd_line, macd_signal = self._calc_macd(df["close"])
        volume_ratio = self._volume_ratio(df["volume"])

        trend = self._determine_trend(rsi, ma_fast, ma_slow, macd_line, macd_signal)
        conviction = self._score_conviction(rsi, ma_fast, ma_slow, macd_line, macd_signal, volume_ratio)

        return TechnicalSignal(
            symbol=symbol,
            rsi=round(float(rsi), 2) if not np.isnan(rsi) else None,
            ma_fast=round(float(ma_fast), 4) if not np.isnan(ma_fast) else None,
            ma_slow=round(float(ma_slow), 4) if not np.isnan(ma_slow) else None,
            macd=round(float(macd_line), 6) if not np.isnan(macd_line) else None,
            macd_signal=round(float(macd_signal), 6) if not np.isnan(macd_signal) else None,
            volume_ratio=round(float(volume_ratio), 2) if not np.isnan(volume_ratio) else None,
            conviction_score=conviction,
            trend=trend,
        )

    def analyze_all(self, data: dict[str, list[list]]) -> list[TechnicalSignal]:
        return [self.analyze(symbol, ohlcv) for symbol, ohlcv in data.items()]

    def _to_dataframe(self, ohlcv: list[list]) -> pd.DataFrame:
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        return df

    def _calc_rsi(self, closes: pd.Series) -> float:
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(self.rsi_period).mean()
        avg_loss = loss.rolling(self.rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return float(val) if not np.isnan(val) else 50.0

    def _calc_macd(self, closes: pd.Series) -> tuple[float, float]:
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        return float(macd.iloc[-1]), float(signal.iloc[-1])

    def _volume_ratio(self, volume: pd.Series, lookback: int = 20) -> float:
        if len(volume) < lookback:
            return 1.0
        avg = volume.iloc[-lookback:-1].mean()
        current = volume.iloc[-1]
        return float(current / avg) if avg > 0 else 1.0

    def _determine_trend(
        self, rsi: float, ma_fast: float, ma_slow: float, macd: float, macd_signal: float
    ) -> str:
        bullish_signals = 0
        bearish_signals = 0

        if ma_fast > ma_slow:
            bullish_signals += 1
        else:
            bearish_signals += 1

        if macd > macd_signal:
            bullish_signals += 1
        else:
            bearish_signals += 1

        if rsi < 30:
            bullish_signals += 1
        elif rsi > 70:
            bearish_signals += 1

        if bullish_signals > bearish_signals:
            return "bullish"
        if bearish_signals > bullish_signals:
            return "bearish"
        return "neutral"

    def _score_conviction(
        self,
        rsi: float,
        ma_fast: float,
        ma_slow: float,
        macd: float,
        macd_signal: float,
        volume_ratio: float,
    ) -> int:
        score = 50

        ma_diff_pct = abs(ma_fast - ma_slow) / ma_slow * 100 if ma_slow else 0
        score += min(15, ma_diff_pct * 3)

        if (macd > macd_signal and ma_fast > ma_slow) or (macd < macd_signal and ma_fast < ma_slow):
            score += 10

        if rsi < 30 or rsi > 70:
            score += 10

        if volume_ratio > 1.5:
            score += 10
        elif volume_ratio < 0.5:
            score -= 10

        return max(0, min(100, int(score)))

    def summarize(self, signals: list[TechnicalSignal]) -> str:
        lines = []
        for s in sorted(signals, key=lambda x: -x.conviction_score):
            parts = [f"{s.symbol}: {s.trend} (conviction {s.conviction_score})"]
            if s.rsi is not None:
                parts.append(f"RSI={s.rsi}")
            lines.append(" | ".join(parts))
        return "\n".join(lines) if lines else "No signals available."
