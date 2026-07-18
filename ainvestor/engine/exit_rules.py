from __future__ import annotations

"""Reglas de salida automáticas (ROE, trailing) y propuestas de cierre obligatorio."""

from ainvestor.config import load_risk_config
from ainvestor.engine.proposal_order import is_close_proposal
from ainvestor.models.schemas import (
    DecisionAction,
    InstrumentType,
    PortfolioSnapshot,
    TechnicalSignal,
    TradeProposal,
)


def _exit_cfg(profile: str) -> dict:
    return load_risk_config(profile=profile).get("exit_rules", {})


def position_trend_aligned(side: str, signal: TechnicalSignal | None) -> bool:
    """True si la tendencia 1h apoya la dirección de la posición."""
    if signal is None:
        return True
    trend = signal.trend_1h or signal.trend or "neutral"
    if side == "long":
        return trend == "bullish"
    if side == "short":
        return trend == "bearish"
    return True


def mandatory_close_proposals(
    snapshot: PortfolioSnapshot,
    signals: dict[str, TechnicalSignal],
    quant_map: dict[str, int],
    profile: str,
) -> list[TradeProposal]:
    """Genera cierres obligatorios antes del ciclo IA (ROE + alineación / convicción)."""
    cfg = _exit_cfg(profile)
    profit_roe = float(cfg.get("take_profit_roe_pct", 12.0))
    loss_roe = float(cfg.get("stop_loss_roe_pct", -5.0))
    loss_min_quant = int(cfg.get("loss_exit_max_quant_conviction", 40))

    proposals: list[TradeProposal] = []
    for pos in snapshot.positions:
        if getattr(pos, "instrument_type", "spot") != "perpetual":
            continue
        roe = pos.roe_pct
        if roe is None:
            continue

        side = getattr(pos, "position_side", "long") or "long"
        signal = signals.get(pos.symbol)
        quant = quant_map.get(pos.symbol, 50)

        reason = ""
        if roe >= profit_roe and not position_trend_aligned(side, signal):
            reason = (
                f"ROE {roe:+.1f}% con tendencia 1h desalineada — cierre obligatorio de beneficio"
            )
        elif roe <= loss_roe and quant < loss_min_quant:
            reason = (
                f"ROE {roe:+.1f}% y convicción quant {quant} < {loss_min_quant} — corte de pérdida"
            )

        if not reason:
            continue

        action = DecisionAction.SELL if side == "long" else DecisionAction.BUY
        proposals.append(
            TradeProposal(
                action=action,
                symbol=pos.symbol,
                amount_pct=100.0,
                stop_loss_pct=10.0,
                take_profit_pct=1.0,
                conviction=90,
                reasoning=reason,
                instrument_type=InstrumentType.PERPETUAL,
                position_side=side,
                leverage=getattr(pos, "leverage", 10) or 10,
            )
        )
    return proposals


def update_trailing_stops(
    positions: list,
    prices: dict[str, float],
    profile: str,
) -> int:
    """
    Ajusta stop_loss hacia arriba/abajo (solo aprieta) cuando ROE supera el umbral de activación.
    Devuelve número de posiciones actualizadas.
    """
    cfg = _exit_cfg(profile)
    activate_roe = float(cfg.get("trailing_activate_roe_pct", 5.0))
    trail_roe = float(cfg.get("trailing_distance_roe_pct", 3.0))

    updated = 0
    for pos in positions:
        if getattr(pos, "instrument_type", "spot") != "perpetual":
            continue
        mark = prices.get(pos.symbol, pos.entry_price)
        if not mark or not pos.entry_price:
            continue

        side = getattr(pos, "position_side", "long") or "long"
        lev = getattr(pos, "leverage", 1) or 1
        margin = pos.margin_used or 0
        if margin <= 0:
            continue

        if side == "long":
            pnl = (mark - pos.entry_price) * pos.amount
        else:
            pnl = (pos.entry_price - mark) * pos.amount
        roe = (pnl / margin) * 100
        if roe < activate_roe:
            continue

        trail_price_pct = trail_roe / lev
        if side == "long":
            new_stop = mark * (1 - trail_price_pct / 100)
            if pos.stop_loss is None or new_stop > pos.stop_loss:
                pos.stop_loss = new_stop
                updated += 1
        else:
            new_stop = mark * (1 + trail_price_pct / 100)
            if pos.stop_loss is None or new_stop < pos.stop_loss:
                pos.stop_loss = new_stop
                updated += 1
    return updated


def is_rotation_open(
    proposal: TradeProposal,
    cycle_proposals: list[TradeProposal],
    snapshot: PortfolioSnapshot,
) -> bool:
    """True si esta propuesta abre tras cerrar otro símbolo en el mismo ciclo."""
    if is_close_proposal(proposal, snapshot):
        return False
    has_close_other = any(
        is_close_proposal(p, snapshot) and p.symbol != proposal.symbol for p in cycle_proposals
    )
    return has_close_other
