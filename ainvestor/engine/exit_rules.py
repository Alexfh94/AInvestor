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


def _target_tp_pct(profile: str) -> float:
    stops = load_risk_config(profile=profile).get("stops", {})
    return float(stops.get("target_take_profit_pct_perp", 1.2))


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
    """
    Cierres obligatorios antes del ciclo IA.

    - Beneficio: ROE >= take_profit_roe_pct (12% ≈ 10% neto tras fees a 10x).
    - Pérdida: ROE <= stop_loss_roe_pct — corte incondicional (sin depender de quant).
    """
    cfg = _exit_cfg(profile)
    profit_roe = float(cfg.get("take_profit_roe_pct", 12.0))
    loss_roe = float(cfg.get("stop_loss_roe_pct", -8.0))
    target_tp = _target_tp_pct(profile)

    proposals: list[TradeProposal] = []
    for pos in snapshot.positions:
        if getattr(pos, "instrument_type", "spot") != "perpetual":
            continue
        roe = pos.roe_pct
        if roe is None:
            continue

        side = getattr(pos, "position_side", "long") or "long"
        lev = getattr(pos, "leverage", 10) or 10

        reason = ""
        if roe >= profit_roe:
            reason = (
                f"ROE {roe:+.1f}% ≥ objetivo {profit_roe:.0f}% "
                f"(~10% neto tras fees a 10x) — take profit obligatorio"
            )
        elif roe <= loss_roe:
            reason = (
                f"ROE {roe:+.1f}% ≤ stop {loss_roe:.0f}% ROE "
                f"— corte de pérdida automático"
            )

        if not reason:
            continue

        action = DecisionAction.SELL if side == "long" else DecisionAction.BUY
        proposals.append(
            TradeProposal(
                action=action,
                symbol=pos.symbol,
                amount_pct=100.0,
                stop_loss_pct=abs(loss_roe) / lev,
                take_profit_pct=target_tp,
                conviction=90,
                reasoning=reason,
                instrument_type=InstrumentType.PERPETUAL,
                position_side=side,
                leverage=lev,
            )
        )
    return proposals


def roe_take_profit_triggers(
    snapshot: PortfolioSnapshot,
    profile: str,
) -> list[tuple[str, float]]:
    """Símbolos con ROE >= objetivo para cierre en risk monitor (entre ciclos IA)."""
    profit_roe = float(_exit_cfg(profile).get("take_profit_roe_pct", 12.0))
    triggers: list[tuple[str, float]] = []
    for pos in snapshot.positions:
        if getattr(pos, "instrument_type", "spot") != "perpetual":
            continue
        if pos.roe_pct is not None and pos.roe_pct >= profit_roe:
            triggers.append((pos.symbol, pos.current_price))
    return triggers


def roe_stop_loss_triggers(
    snapshot: PortfolioSnapshot,
    profile: str,
) -> list[tuple[str, float]]:
    """Símbolos con ROE <= stop para corte en risk monitor (entre ciclos IA)."""
    loss_roe = float(_exit_cfg(profile).get("stop_loss_roe_pct", -8.0))
    triggers: list[tuple[str, float]] = []
    for pos in snapshot.positions:
        if getattr(pos, "instrument_type", "spot") != "perpetual":
            continue
        if pos.roe_pct is not None and pos.roe_pct <= loss_roe:
            triggers.append((pos.symbol, pos.current_price))
    return triggers


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
    activate_roe = float(cfg.get("trailing_activate_roe_pct", 8.0))
    trail_roe = float(cfg.get("trailing_distance_roe_pct", 4.0))

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
