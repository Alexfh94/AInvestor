"""Human-readable labels for trade records."""
from __future__ import annotations


def infer_trade_action(
    *,
    trade_action: str | None,
    instrument_type: str,
    position_side: str,
    side: str,
) -> str:
    if trade_action in ("open", "close"):
        return trade_action
    if instrument_type != "perpetual":
        return "open" if side == "buy" else "close"
    if side == "buy" and position_side == "long":
        return "open"
    if side == "sell" and position_side == "short":
        return "open"
    return "close"


def format_trade_operation(
    *,
    trade_action: str | None = None,
    instrument_type: str = "spot",
    position_side: str = "long",
    side: str = "buy",
) -> str:
    action = infer_trade_action(
        trade_action=trade_action,
        instrument_type=instrument_type,
        position_side=position_side,
        side=side,
    )
    if instrument_type != "perpetual":
        if action == "open":
            return "Comprar SPOT"
        return "Vender SPOT"
    side_label = (position_side or "long").upper()
    verb = "Abrir" if action == "open" else "Cerrar"
    return f"{verb} {side_label}"


def trade_to_api_dict(trade) -> dict:
    from ainvestor.utils.datetime_utils import format_app_datetime

    instrument_type = getattr(trade, "instrument_type", "spot") or "spot"
    position_side = getattr(trade, "position_side", "long") or "long"
    leverage = getattr(trade, "leverage", 1) or 1
    trade_action = getattr(trade, "trade_action", None)
    if not trade_action:
        trade_action = infer_trade_action(
            trade_action=None,
            instrument_type=instrument_type,
            position_side=position_side,
            side=trade.side,
        )
    margin_used = (
        trade.value_usdt / leverage
        if instrument_type == "perpetual" and leverage > 1
        else None
    )
    if trade_action == "open" and margin_used is None and instrument_type == "perpetual":
        margin_used = trade.value_usdt / max(leverage, 1)

    return {
        "id": trade.id,
        "symbol": trade.symbol,
        "side": trade.side,
        "trade_action": trade_action,
        "operation_label": format_trade_operation(
            trade_action=trade_action,
            instrument_type=instrument_type,
            position_side=position_side,
            side=trade.side,
        ),
        "amount": trade.amount,
        "price": trade.price,
        "value_usdt": trade.value_usdt,
        "fee": trade.fee,
        "mode": trade.mode,
        "status": trade.status,
        "instrument_type": instrument_type,
        "position_side": position_side,
        "leverage": leverage,
        "margin_used": margin_used,
        "realized_pnl_usdt": getattr(trade, "realized_pnl_usdt", None),
        "pnl_pct_roe": getattr(trade, "pnl_pct_roe", None),
        "executed_at": format_app_datetime(trade.executed_at),
    }
