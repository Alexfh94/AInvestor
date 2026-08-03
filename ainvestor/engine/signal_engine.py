from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ainvestor.config import load_risk_config
from ainvestor.engine.exit_rules import trend_reversal_close_proposal
from ainvestor.models.schemas import (
    CycleDecision,
    DecisionAction,
    DerivativesSnapshot,
    InstrumentType,
    PortfolioSnapshot,
    TechnicalSignal,
    TradeProposal,
)

logger = logging.getLogger(__name__)


@dataclass
class SignalEvaluation:
    """Result of deterministic signal evaluation for one cycle."""

    decision: CycleDecision
    signals: list[TechnicalSignal] = field(default_factory=list)
    selected_symbol: str | None = None
    selected_side: str | None = None
    selected_score: int = 0
    snapshots: list[dict] = field(default_factory=list)


class SignalEngine:
    """Deterministic entry engine: ranks long/short setups and emits trade proposals."""

    def __init__(self, profile: str = "extreme"):
        self.profile = profile
        self.config = load_risk_config(profile=profile)
        signal_cfg = self.config.get("signal_engine", {})
        self.min_entry_score = int(signal_cfg.get("min_entry_score", 72))
        self.min_adx = int(signal_cfg.get("min_adx", 18))
        self.required_leverage = int(
            self.config.get("derivatives", {}).get("max_leverage", 20)
        )
        exit_cfg = self.config.get("exit_rules", {})
        self.stop_loss_roe = abs(float(exit_cfg.get("stop_loss_roe_pct", -16.0)))

    def evaluate(
        self,
        signals: list[TechnicalSignal],
        deriv_by_symbol: dict[str, DerivativesSnapshot],
        snapshot: PortfolioSnapshot,
        oi_delta_by_symbol: dict[str, float | None] | None = None,
    ) -> SignalEvaluation:
        oi_delta_by_symbol = oi_delta_by_symbol or {}
        snapshots: list[dict] = []
        ranked: list[tuple[str, str, int, str]] = []

        for sig in signals:
            funding_rate = 0.0
            deriv = deriv_by_symbol.get(sig.symbol)
            if deriv:
                funding_rate = deriv.funding_rate

            long_score, short_score, reason = self._apply_derivatives_bonus(
                sig, funding_rate, oi_delta_by_symbol.get(sig.symbol)
            )
            sig.long_score = long_score
            sig.short_score = short_score
            sig.conviction_score = max(long_score, short_score)
            sig.entry_reason = reason

            snapshots.append(self._snapshot_row(sig, funding_rate))

            if not sig.tradable:
                continue

            if long_score >= self.min_entry_score:
                ranked.append((sig.symbol, "long", long_score, reason))
            if short_score >= self.min_entry_score:
                ranked.append((sig.symbol, "short", short_score, reason))

        ranked.sort(key=lambda x: -x[2])

        open_perps = [
            p for p in snapshot.positions if getattr(p, "instrument_type", "spot") == "perpetual"
        ]

        if open_perps:
            pos = open_perps[0]
            sig = next((s for s in signals if s.symbol == pos.symbol), None)
            reversal = trend_reversal_close_proposal(pos, sig, self.profile)
            if reversal:
                summary = (
                    f"Cierre {pos.symbol} {getattr(pos, 'position_side', 'long')}: "
                    f"{reversal.reasoning}"
                )
                return SignalEvaluation(
                    decision=CycleDecision(hold=False, summary=summary, proposals=[reversal]),
                    signals=signals,
                    selected_symbol=pos.symbol,
                    selected_side=getattr(pos, "position_side", "long"),
                    selected_score=reversal.conviction,
                    snapshots=snapshots,
                )
            return SignalEvaluation(
                decision=CycleDecision(
                    hold=True,
                    summary=self._hold_summary(open_perps[0]),
                    proposals=[],
                ),
                signals=signals,
                snapshots=snapshots,
            )

        if not ranked:
            return SignalEvaluation(
                decision=CycleDecision(
                    hold=True,
                    summary=(
                        f"Sin setup con score ≥ {self.min_entry_score} "
                        f"(ADX≥{self.min_adx}, MTF 1h+4h requeridos). Permanece en cash."
                    ),
                    proposals=[],
                ),
                signals=signals,
                snapshots=snapshots,
            )

        symbol, side, score, reason = ranked[0]
        proposal = self._build_proposal(symbol, side, score, reason)
        summary = (
            f"Entrada {side.upper()} {symbol} score={score} — {reason}. "
            f"Leverage {self.required_leverage}x all-in."
        )

        return SignalEvaluation(
            decision=CycleDecision(hold=False, summary=summary, proposals=[proposal]),
            signals=signals,
            selected_symbol=symbol,
            selected_side=side,
            selected_score=score,
            snapshots=snapshots,
        )

    def _apply_derivatives_bonus(
        self,
        sig: TechnicalSignal,
        funding_rate: float,
        oi_delta: float | None,
    ) -> tuple[int, int, str]:
        long_score = sig.long_score
        short_score = sig.short_score
        reasons = [sig.entry_reason] if sig.entry_reason else []

        warning_pct = (
            float(self.config.get("derivatives", {}).get("funding_cost_warning_pct", 0.20))
            / 100
        )

        if funding_rate < 0:
            long_score += 10
            reasons.append("funding favorece long")
        elif funding_rate > warning_pct:
            long_score = max(0, long_score - 15)
            short_score += 10
            reasons.append("funding favorece short")
        elif funding_rate > 0:
            short_score += 5

        if oi_delta is not None:
            if oi_delta > 2.0:
                if sig.trend == "bullish":
                    long_score += 5
                elif sig.trend == "bearish":
                    short_score += 5
                reasons.append(f"OI+{oi_delta:.1f}%")
            elif oi_delta < -2.0:
                long_score = max(0, long_score - 5)
                short_score = max(0, short_score - 5)
                reasons.append(f"OI{oi_delta:.1f}%")

        long_score = max(0, min(100, long_score))
        short_score = max(0, min(100, short_score))
        return long_score, short_score, "; ".join(r for r in reasons if r)

    def _build_proposal(
        self, symbol: str, side: str, score: int, reason: str
    ) -> TradeProposal:
        sl_price_pct = self.stop_loss_roe / self.required_leverage
        action = DecisionAction.BUY if side == "long" else DecisionAction.SELL
        return TradeProposal(
            action=action,
            symbol=symbol,
            amount_pct=100.0,
            stop_loss_pct=sl_price_pct,
            take_profit_pct=0.0,
            conviction=score,
            reasoning=reason,
            instrument_type=InstrumentType.PERPETUAL,
            position_side=side,  # type: ignore[arg-type]
            leverage=self.required_leverage,
        )

    def _hold_summary(self, position) -> str:
        side = getattr(position, "position_side", "long")
        roe = getattr(position, "roe_pct", None)
        roe_str = f"{roe:+.1f}%" if roe is not None else "N/A"
        return (
            f"Posición abierta {position.symbol} {side} — ROE {roe_str}. "
            f"Mantener hasta TP (+24% ROE) o SL (−16% ROE); "
            f"cierre anticipado solo si reversión de tendencia confirmada."
        )

    def _snapshot_row(self, sig: TechnicalSignal, funding_rate: float) -> dict:
        return {
            "symbol": sig.symbol,
            "long_score": sig.long_score,
            "short_score": sig.short_score,
            "adx": sig.adx,
            "mtf_alignment": sig.mtf_aligned,
            "funding_rate": funding_rate,
            "entry_reason": sig.entry_reason,
            "trend_1h": sig.trend_1h,
            "trend_4h": sig.trend_4h,
            "trend_1d": sig.trend_1d,
        }

    def snapshots_to_json(self, snapshots: list[dict]) -> str:
        return json.dumps(snapshots)
