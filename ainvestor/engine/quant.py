from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from ainvestor.config import load_risk_config
from ainvestor.models.schemas import TechnicalSignal

logger = logging.getLogger(__name__)


class QuantEngine:
    """Technical analysis with directional long/short scoring and MTF trend filters."""

    def __init__(
        self,
        rsi_period: int = 14,
        ema_fast: int = 21,
        ema_slow: int = 50,
        atr_period: int = 14,
        adx_period: int = 14,
        profile: str = "extreme",
    ):
        self.rsi_period = rsi_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.profile = profile
        cfg = load_risk_config(profile=profile)
        signal_cfg = cfg.get("signal_engine", {})
        self.min_adx = float(signal_cfg.get("min_adx", 22))
        self.min_bars = self.ema_slow + 5

    def analyze(self, symbol: str, ohlcv: list[list]) -> TechnicalSignal:
        if len(ohlcv) < self.min_bars:
            return TechnicalSignal(symbol=symbol, conviction_score=50, trend="neutral")

        df = self._to_dataframe(ohlcv)
        indicators = self._compute_indicators(df)
        trend = self._determine_trend_from_ema(indicators)
        long_score, short_score, reason = self._score_directional(indicators, trend)

        conviction = max(long_score, short_score)
        tradable = indicators["adx"] >= self.min_adx and max(long_score, short_score) > 0

        return TechnicalSignal(
            symbol=symbol,
            rsi=round(indicators["rsi"], 2),
            ma_fast=round(indicators["ema_fast"], 4),
            ma_slow=round(indicators["ema_slow"], 4),
            ema_fast=round(indicators["ema_fast"], 4),
            ema_slow=round(indicators["ema_slow"], 4),
            macd=round(indicators["macd"], 6),
            macd_signal=round(indicators["macd_signal"], 6),
            macd_histogram=round(indicators["macd_histogram"], 6),
            adx=round(indicators["adx"], 2),
            volume_ratio=round(indicators["volume_ratio"], 2),
            atr=round(indicators["atr"], 6) if indicators["atr"] else None,
            atr_pct=round(indicators["atr_pct"], 2) if indicators["atr_pct"] else None,
            session_change_pct=round(indicators["session_change"], 2)
            if indicators["session_change"] is not None
            else None,
            trend_1h=trend,
            long_score=long_score,
            short_score=short_score,
            conviction_score=conviction,
            trend=trend,
            tradable=tradable,
            entry_reason=reason,
        )

    def analyze_multi_timeframe(
        self, symbol: str, ohlcv_by_tf: dict[str, list[list]]
    ) -> TechnicalSignal:
        primary = ohlcv_by_tf.get("1h") or next(iter(ohlcv_by_tf.values()), [])
        signal = self.analyze(symbol, primary)

        tf_trends: dict[str, str] = {}
        for tf_key, attr in (("4h", "trend_4h"), ("1d", "trend_1d")):
            ohlcv = ohlcv_by_tf.get(tf_key)
            if ohlcv and len(ohlcv) >= self.min_bars:
                df = self._to_dataframe(ohlcv)
                indicators = self._compute_indicators(df)
                tf_trend = self._determine_trend_from_ema(indicators)
                tf_trends[tf_key] = tf_trend
                setattr(signal, attr, tf_trend)

        trends = [signal.trend_1h, signal.trend_4h, signal.trend_1d]
        non_neutral = [t for t in trends if t and t != "neutral"]
        signal.mtf_aligned = (
            len(non_neutral) >= 3 and all(t == non_neutral[0] for t in non_neutral)
        )

        long_score, short_score, reason = self._apply_mtf_scoring(
            signal, tf_trends, signal.long_score, signal.short_score
        )
        signal.long_score = long_score
        signal.short_score = short_score
        signal.conviction_score = max(long_score, short_score)
        signal.entry_reason = reason
        signal.tradable = (
            signal.adx is not None
            and signal.adx >= self.min_adx
            and signal.mtf_aligned
            and max(long_score, short_score) > 0
        )

        if signal.mtf_aligned and non_neutral:
            signal.trend = non_neutral[0]

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

    def _compute_indicators(self, df: pd.DataFrame) -> dict[str, Any]:
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        ema_fast = close.ewm(span=self.ema_fast, adjust=False).mean().iloc[-1]
        ema_slow = close.ewm(span=self.ema_slow, adjust=False).mean().iloc[-1]
        rsi = self._calc_rsi(close)
        macd_line, macd_signal, macd_hist = self._calc_macd(close)
        atr, atr_pct = self._calc_atr(df)
        adx = self._calc_adx(df)
        volume_ratio = self._volume_ratio(volume)
        session_change = self._session_momentum(df)

        return {
            "rsi": float(rsi) if not np.isnan(rsi) else 50.0,
            "ema_fast": float(ema_fast) if not np.isnan(ema_fast) else 0.0,
            "ema_slow": float(ema_slow) if not np.isnan(ema_slow) else 0.0,
            "macd": float(macd_line) if not np.isnan(macd_line) else 0.0,
            "macd_signal": float(macd_signal) if not np.isnan(macd_signal) else 0.0,
            "macd_histogram": float(macd_hist) if not np.isnan(macd_hist) else 0.0,
            "atr": float(atr) if atr and not np.isnan(atr) else None,
            "atr_pct": float(atr_pct) if atr_pct and not np.isnan(atr_pct) else None,
            "adx": float(adx) if not np.isnan(adx) else 0.0,
            "volume_ratio": float(volume_ratio) if not np.isnan(volume_ratio) else 1.0,
            "session_change": session_change,
        }

    def _session_momentum(self, df: pd.DataFrame, lookback: int = 6) -> float | None:
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

    def _calc_macd(self, closes: pd.Series) -> tuple[float, float, float]:
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        hist = macd - signal
        return float(macd.iloc[-1]), float(signal.iloc[-1]), float(hist.iloc[-1])

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

    def _calc_adx(self, df: pd.DataFrame) -> float:
        if len(df) < self.adx_period * 2:
            return 0.0
        high = df["high"]
        low = df["low"]
        close = df["close"]
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(self.adx_period).mean()
        plus_di = 100 * (plus_dm.rolling(self.adx_period).mean() / atr.replace(0, np.nan))
        minus_di = 100 * (minus_dm.rolling(self.adx_period).mean() / atr.replace(0, np.nan))
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        adx = dx.rolling(self.adx_period).mean().iloc[-1]
        return float(adx) if not np.isnan(adx) else 0.0

    def _volume_ratio(self, volume: pd.Series, lookback: int = 20) -> float:
        if len(volume) < lookback:
            return 1.0
        avg = volume.iloc[-lookback:-1].mean()
        current = volume.iloc[-1]
        return float(current / avg) if avg > 0 else 1.0

    def _determine_trend_from_ema(self, indicators: dict[str, Any]) -> str:
        ema_fast = indicators["ema_fast"]
        ema_slow = indicators["ema_slow"]
        macd_hist = indicators["macd_histogram"]
        if ema_fast > ema_slow and macd_hist >= 0:
            return "bullish"
        if ema_fast < ema_slow and macd_hist <= 0:
            return "bearish"
        return "neutral"

    def _score_directional(
        self, indicators: dict[str, Any], trend: str
    ) -> tuple[int, int, str]:
        rsi = indicators["rsi"]
        macd_hist = indicators["macd_histogram"]
        volume_ratio = indicators["volume_ratio"]
        session_change = indicators["session_change"]
        adx = indicators["adx"]

        long_score = 0
        short_score = 0
        reasons: list[str] = []

        if adx < self.min_adx:
            return 0, 0, f"ADX {adx:.1f} < {self.min_adx:.0f} (rango)"

        if trend == "bullish":
            long_score += 20
            reasons.append("1h bullish")
        elif trend == "bearish":
            short_score += 20
            reasons.append("1h bearish")

        if macd_hist > 0:
            long_score += 15
            if macd_hist > 0:
                reasons.append("MACD+")
        elif macd_hist < 0:
            short_score += 15
            reasons.append("MACD-")

        if volume_ratio > 1.3:
            long_score += 10
            short_score += 10
            reasons.append("vol>1.3x")

        if session_change is not None:
            if session_change > 0.5:
                long_score += 10
            elif session_change < -0.5:
                short_score += 10

        if rsi > 65:
            long_score -= 20
            reasons.append("RSI overbought")
        elif rsi < 35:
            short_score -= 20
            reasons.append("RSI oversold")

        if rsi < 35 and trend == "bullish":
            long_score += 5
        if rsi > 65 and trend == "bearish":
            short_score += 5

        long_score = max(0, min(100, long_score))
        short_score = max(0, min(100, short_score))
        return long_score, short_score, "; ".join(reasons)

    def _apply_mtf_scoring(
        self,
        signal: TechnicalSignal,
        tf_trends: dict[str, str],
        long_score: int,
        short_score: int,
    ) -> tuple[int, int, str]:
        reasons = [signal.entry_reason] if signal.entry_reason else []
        trends = [signal.trend_1h, signal.trend_4h, signal.trend_1d]
        non_neutral = [t for t in trends if t and t != "neutral"]

        if signal.mtf_aligned and non_neutral:
            direction = non_neutral[0]
            if direction == "bullish":
                long_score += 25
                short_score = max(0, short_score - 30)
                reasons.append("MTF 3/3 bullish")
            else:
                short_score += 25
                long_score = max(0, long_score - 30)
                reasons.append("MTF 3/3 bearish")
        else:
            long_score = max(0, long_score - 15)
            short_score = max(0, short_score - 15)
            if signal.trend_4h and signal.trend_1h != signal.trend_4h:
                long_score = max(0, long_score - 10)
                short_score = max(0, short_score - 10)
                reasons.append("1h≠4h")
            if signal.trend_1d and signal.trend_1h != signal.trend_1d:
                long_score = max(0, long_score - 8)
                short_score = max(0, short_score - 8)
                reasons.append("1h≠1d")

        long_score = max(0, min(100, long_score))
        short_score = max(0, min(100, short_score))
        return long_score, short_score, "; ".join(r for r in reasons if r)

    def summarize(self, signals: list[TechnicalSignal]) -> str:
        lines = []
        for s in sorted(signals, key=lambda x: -max(x.long_score, x.short_score)):
            best = "long" if s.long_score >= s.short_score else "short"
            best_score = max(s.long_score, s.short_score)
            parts = [
                f"{s.symbol}: {best} {best_score} (L{s.long_score}/S{s.short_score})",
                f"trend={s.trend}",
            ]
            if s.adx is not None:
                parts.append(f"ADX={s.adx}")
            if s.rsi is not None:
                parts.append(f"RSI={s.rsi}")
            if s.atr_pct is not None:
                parts.append(f"ATR%={s.atr_pct}")
            mtf = []
            if s.trend_4h:
                mtf.append(f"4h={s.trend_4h}")
            if s.trend_1d:
                mtf.append(f"1d={s.trend_1d}")
            if mtf:
                parts.append(" ".join(mtf))
            if s.mtf_aligned:
                parts.append("MTF✓")
            lines.append(" | ".join(parts))
        return "\n".join(lines) if lines else "No signals available."

    def suggest_stops_from_atr(
        self, signal: TechnicalSignal, atr_multiplier_sl: float = 1.5, atr_multiplier_tp: float = 2.5
    ) -> dict[str, float] | None:
        if signal.atr_pct is None:
            return None
        return {
            "stop_loss_pct": round(signal.atr_pct * atr_multiplier_sl, 2),
            "take_profit_pct": round(min(signal.atr_pct * atr_multiplier_tp, 1.5), 2),
        }
