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


def is_loss_stop_price(
    *,
    position_side: str,
    entry_price: float,
    stop_loss: float,
) -> bool:
    """True si stop_loss es el SL de pérdida inicial (por debajo/encima de entrada)."""
    if position_side == "long":
        return stop_loss <= entry_price
    return stop_loss >= entry_price


def perp_loss_stop_price(
    entry_price: float,
    position_side: str,
    leverage: int,
    profile: str,
) -> float:
    """Precio de stop-loss de pérdida fijo (siempre por debajo/encima de la entrada)."""
    loss_roe = abs(float(_exit_cfg(profile).get("stop_loss_roe_pct", -16.0)))
    sl_price_pct = loss_roe / max(leverage, 1)
    if position_side == "long":
        return entry_price * (1 - sl_price_pct / 100)
    return entry_price * (1 + sl_price_pct / 100)


def normalize_perp_stop_loss(position, profile: str) -> bool:
    """
    Corrige stops de perps que quedaron en zona de beneficio (trailing legacy).
    Devuelve True si se actualizó el stop en BD.
    """
    if getattr(position, "instrument_type", "spot") != "perpetual":
        return False
    entry = position.entry_price
    if not entry:
        return False
    side = getattr(position, "position_side", "long") or "long"
    lev = int(getattr(position, "leverage", 20) or 20)
    target = perp_loss_stop_price(entry, side, lev, profile)
    if position.stop_loss is None:
        position.stop_loss = target
        return True
    if not is_loss_stop_price(
        position_side=side, entry_price=entry, stop_loss=position.stop_loss
    ):
        position.stop_loss = target
        return True
    return False


def normalize_open_perp_stops(positions: list, profile: str) -> int:
    return sum(1 for pos in positions if normalize_perp_stop_loss(pos, profile))


def collect_price_stop_triggers(
    snapshot: PortfolioSnapshot,
    profile: str,
) -> list[tuple[str, str, float, str]]:
    """
    Disparos por precio vs stop_loss/take_profit.

    Perps: no usan SL/TP por precio — solo salidas por ROE (TP/SL) en el monitor.
    Spot: mantiene stops por precio.
    """
    triggers: list[tuple[str, str, float, str]] = []

    for pos in snapshot.positions:
        if getattr(pos, "instrument_type", "spot") == "perpetual":
            continue
        side = getattr(pos, "position_side", "long") or "long"
        mark = pos.current_price
        if not mark:
            continue
        if side == "short":
            if pos.stop_loss and mark >= pos.stop_loss:
                triggers.append((pos.symbol, "sell", mark, "risk_sl"))
            elif pos.take_profit and mark <= pos.take_profit:
                triggers.append((pos.symbol, "sell", mark, "risk_tp"))
        else:
            if pos.stop_loss and mark <= pos.stop_loss:
                triggers.append((pos.symbol, "sell", mark, "risk_sl"))
            elif pos.take_profit and mark >= pos.take_profit:
                triggers.append((pos.symbol, "sell", mark, "risk_tp"))
    return triggers


def trend_reversal_close_proposal(
    position,
    signal: TechnicalSignal | None,
    profile: str,
) -> TradeProposal | None:
    """
    Cierre anticipado solo si la señal contraria es muy fuerte (reversión de tendencia).
    No cierra en beneficio pequeño: solo cuando el quant indica riesgo claro de pérdida.
    """
    if signal is None:
        return None
    cfg = _exit_cfg(profile)
    min_score = int(cfg.get("trend_reversal_min_score", 75))
    min_delta = int(cfg.get("trend_reversal_score_delta", 25))
    min_adx = float(cfg.get("trend_reversal_min_adx", 22))
    adx = signal.adx or 0.0
    if adx < min_adx:
        return None

    side = getattr(position, "position_side", "long") or "long"
    lev = int(getattr(position, "leverage", 20) or 20)
    loss_roe = float(cfg.get("stop_loss_roe_pct", -16.0))
    roe = getattr(position, "roe_pct", None)

    if side == "long":
        if signal.short_score < min_score:
            return None
        if signal.short_score < signal.long_score + min_delta:
            return None
        if (signal.trend_4h or signal.trend) != "bearish":
            return None
        if (signal.trend_1h or "neutral") == "bullish":
            return None
        reason = (
            f"Reversión bajista confirmada: short_score={signal.short_score} "
            f"(long={signal.long_score}), ADX={adx:.0f}, 4h bearish — "
            f"cierre anticipado (ROE actual {roe:+.1f}% si aplica)"
            if roe is not None
            else f"Reversión bajista confirmada: short_score={signal.short_score}, "
            f"ADX={adx:.0f}, 4h bearish — cierre anticipado"
        )
        action = DecisionAction.SELL
    else:
        if signal.long_score < min_score:
            return None
        if signal.long_score < signal.short_score + min_delta:
            return None
        if (signal.trend_4h or signal.trend) != "bullish":
            return None
        if (signal.trend_1h or "neutral") == "bearish":
            return None
        reason = (
            f"Reversión alcista confirmada: long_score={signal.long_score} "
            f"(short={signal.short_score}), ADX={adx:.0f}, 4h bullish — "
            f"cierre anticipado (ROE actual {roe:+.1f}% si aplica)"
            if roe is not None
            else f"Reversión alcista confirmada: long_score={signal.long_score}, "
            f"ADX={adx:.0f}, 4h bullish — cierre anticipado"
        )
        action = DecisionAction.BUY

    return TradeProposal(
        action=action,
        symbol=position.symbol,
        amount_pct=100.0,
        stop_loss_pct=abs(loss_roe) / lev,
        take_profit_pct=0.0,
        conviction=signal.short_score if side == "long" else signal.long_score,
        reasoning=reason,
        instrument_type=InstrumentType.PERPETUAL,
        position_side=side,
        leverage=lev,
    )


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
    Cierres obligatorios antes del ciclo.

    - Beneficio: ROE >= take_profit_roe_pct (24% ≈ 20% neto tras fees a 20x).
    - Pérdida: ROE <= stop_loss_roe_pct — corte incondicional.
    """
    cfg = _exit_cfg(profile)
    profit_roe = float(cfg.get("take_profit_roe_pct", 24.0))
    loss_roe = float(cfg.get("stop_loss_roe_pct", -16.0))

    proposals: list[TradeProposal] = []
    for pos in snapshot.positions:
        if getattr(pos, "instrument_type", "spot") != "perpetual":
            continue
        roe = pos.roe_pct
        if roe is None:
            continue

        side = getattr(pos, "position_side", "long") or "long"
        lev = getattr(pos, "leverage", 20) or 20

        reason = ""
        if roe >= profit_roe:
            reason = (
                f"ROE {roe:+.1f}% ≥ objetivo {profit_roe:.0f}% "
                f"(~10% neto tras fees a 20x) — take profit obligatorio"
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
                take_profit_pct=0.0,
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
    profit_roe = float(_exit_cfg(profile).get("take_profit_roe_pct", 24.0))
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
    loss_roe = float(_exit_cfg(profile).get("stop_loss_roe_pct", -16.0))
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
    if not cfg.get("trailing_enabled", False):
        return 0
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
