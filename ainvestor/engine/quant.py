from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from ainvestor.models.schemas import TechnicalSignal

logger = logging.getLogger(__name__)


class QuantEngine:
    """Technical analysis: RSI, MA, MACD, volume, ATR, multi-timeframe."""

    def __init__(self, rsi_period: int = 14, ma_fast: int = 9, ma_slow: int = 21, atr_period: int = 14):
        self.rsi_period = rsi_period
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.atr_period = atr_period

    def analyze(self, symbol: str, ohlcv: list[list]) -> TechnicalSignal:
        if len(ohlcv) < self.ma_slow + 5:
            return TechnicalSignal(symbol=symbol, conviction_score=50, trend="neutral")

        df = self._to_dataframe(ohlcv)
        rsi = self._calc_rsi(df["close"])
        ma_fast = df["close"].rolling(self.ma_fast).mean().iloc[-1]
        ma_slow = df["close"].rolling(self.ma_slow).mean().iloc[-1]
        macd_line, macd_signal = self._calc_macd(df["close"])
        volume_ratio = self._volume_ratio(df["volume"])
        atr, atr_pct = self._calc_atr(df)
        session_change = self._session_momentum(df)

        trend = self._determine_trend(rsi, ma_fast, ma_slow, macd_line, macd_signal)
        conviction = self._score_conviction(
            rsi,
            ma_fast,
            ma_slow,
            macd_line,
            macd_signal,
            volume_ratio,
            atr_pct,
            trend,
            session_change,
        )

        return TechnicalSignal(
            symbol=symbol,
            rsi=round(float(rsi), 2) if not np.isnan(rsi) else None,
            ma_fast=round(float(ma_fast), 4) if not np.isnan(ma_fast) else None,
            ma_slow=round(float(ma_slow), 4) if not np.isnan(ma_slow) else None,
            macd=round(float(macd_line), 6) if not np.isnan(macd_line) else None,
            macd_signal=round(float(macd_signal), 6) if not np.isnan(macd_signal) else None,
            volume_ratio=round(float(volume_ratio), 2) if not np.isnan(volume_ratio) else None,
            atr=round(float(atr), 6) if atr and not np.isnan(atr) else None,
            atr_pct=round(float(atr_pct), 2) if atr_pct and not np.isnan(atr_pct) else None,
            session_change_pct=round(session_change, 2) if session_change is not None else None,
            trend_1h=trend,
            conviction_score=conviction,
            trend=trend,
        )

    def analyze_multi_timeframe(
        self, symbol: str, ohlcv_by_tf: dict[str, list[list]]
    ) -> TechnicalSignal:
        primary = ohlcv_by_tf.get("1h") or next(iter(ohlcv_by_tf.values()), [])
        signal = self.analyze(symbol, primary)

        for tf_key, attr in (("4h", "trend_4h"), ("1d", "trend_1d")):
            ohlcv = ohlcv_by_tf.get(tf_key)
            if ohlcv and len(ohlcv) >= self.ma_slow + 5:
                df = self._to_dataframe(ohlcv)
                rsi = self._calc_rsi(df["close"])
                ma_fast = df["close"].rolling(self.ma_fast).mean().iloc[-1]
                ma_slow = df["close"].rolling(self.ma_slow).mean().iloc[-1]
                macd_line, macd_signal = self._calc_macd(df["close"])
                tf_trend = self._determine_trend(rsi, ma_fast, ma_slow, macd_line, macd_signal)
                setattr(signal, attr, tf_trend)

        trends = [t for t in [signal.trend_1h, signal.trend_4h, signal.trend_1d] if t]
        non_neutral = [t for t in trends if t != "neutral"]
        if len(non_neutral) >= 2 and all(t == non_neutral[0] for t in non_neutral):
            signal.conviction_score = min(100, signal.conviction_score + 15)
            signal.trend = non_neutral[0]
        elif signal.trend_4h and signal.trend_1h != signal.trend_4h:
            signal.conviction_score = max(0, signal.conviction_score - 12)
        if signal.trend_1d and signal.trend_1h != signal.trend_1d:
            signal.conviction_score = max(0, signal.conviction_score - 8)

        return signal

    def analyze_all(self, data: dict[str, list[list]]) -> list[TechnicalSignal]:
        return [self.analyze(symbol, ohlcv) for symbol, ohlcv in data.items()]

    def analyze_all_multi(self, data: dict[str, dict[str, list[list]]]) -> list[TechnicalSignal]:
        return [
            self.analyze_multi_timeframe(symbol, ohlcv_by_tf)
            for symbol, ohlcv_by_tf in data.items()
        ]

    def get_quant_conviction_map(self, signals: list[TechnicalSignal]) -> dict[str, int]:
        return {s.symbol: s.conviction_score for s in signals}

    def signals_by_symbol(self, signals: list[TechnicalSignal]) -> dict[str, TechnicalSignal]:
        return {s.symbol: s for s in signals}

    def _to_dataframe(self, ohlcv: list[list]) -> pd.DataFrame:
        return pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])

    def _session_momentum(self, df: pd.DataFrame, lookback: int = 6) -> float | None:
        """Cambio % de precio en las últimas velas 1h (sesión reciente)."""
        if len(df) < lookback:
            return None
        start = float(df["close"].iloc[-lookback])
        end = float(df["close"].iloc[-1])
        if start <= 0:
            return None
        return (end - start) / start * 100

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

    def _calc_atr(self, df: pd.DataFrame) -> tuple[float | None, float | None]:
        if len(df) < self.atr_period + 1:
            return None, None
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(self.atr_period).mean().iloc[-1]
        last_close = close.iloc[-1]
        atr_pct = (atr / last_close * 100) if last_close > 0 else None
        return float(atr), float(atr_pct) if atr_pct is not None else None

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
        atr_pct: float | None = None,
        trend: str = "neutral",
        session_change: float | None = None,
    ) -> int:
        score = 50

        ma_diff_pct = abs(ma_fast - ma_slow) / ma_slow * 100 if ma_slow else 0
        score += min(15, ma_diff_pct * 3)

        if (macd > macd_signal and ma_fast > ma_slow) or (macd < macd_signal and ma_fast < ma_slow):
            score += 10

        if rsi < 30 or rsi > 70:
            score += 5

        if trend == "bullish" and rsi > 75:
            score -= 15
        elif trend == "bearish" and rsi < 25:
            score -= 15

        if volume_ratio > 1.5:
            score += 10
        elif volume_ratio < 0.5:
            score -= 10

        if atr_pct is not None:
            if atr_pct > 5:
                score -= 5
            elif atr_pct < 2:
                score += 5

        if session_change is not None:
            if trend == "bullish" and session_change > 0.5:
                score += min(10, int(session_change * 2))
            elif trend == "bearish" and session_change < -0.5:
                score += min(10, int(abs(session_change) * 2))
            elif trend == "bullish" and session_change < -1.0:
                score -= 10
            elif trend == "bearish" and session_change > 1.0:
                score -= 10

        return max(0, min(100, int(score)))

    def summarize(self, signals: list[TechnicalSignal]) -> str:
        lines = []
        for s in sorted(signals, key=lambda x: -x.conviction_score):
            parts = [f"{s.symbol}: {s.trend} (conviction {s.conviction_score})"]
            if s.rsi is not None:
                parts.append(f"RSI={s.rsi}")
            if s.atr_pct is not None:
                parts.append(f"ATR%={s.atr_pct}")
            if s.session_change_pct is not None:
                parts.append(f"session={s.session_change_pct:+.1f}%")
            mtf = []
            if s.trend_4h:
                mtf.append(f"4h={s.trend_4h}")
            if s.trend_1d:
                mtf.append(f"1d={s.trend_1d}")
            if mtf:
                parts.append(" ".join(mtf))
            lines.append(" | ".join(parts))
        return "\n".join(lines) if lines else "No signals available."

    def suggest_stops_from_atr(
        self, signal: TechnicalSignal, atr_multiplier_sl: float = 1.5, atr_multiplier_tp: float = 2.5
    ) -> dict[str, float] | None:
        if signal.atr_pct is None:
            return None
        tp = min(signal.atr_pct * atr_multiplier_tp, 1.5)
        return {
            "stop_loss_pct": round(signal.atr_pct * atr_multiplier_sl, 2),
            "take_profit_pct": round(tp, 2),
        }
