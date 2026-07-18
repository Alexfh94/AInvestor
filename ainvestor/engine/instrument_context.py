from __future__ import annotations



from sqlalchemy import desc

from sqlalchemy.orm import Session



from ainvestor.config import get_settings, load_risk_config

from ainvestor.db.models import DerivativesRecord

from ainvestor.engine.quant import QuantEngine

from ainvestor.models.schemas import DerivativesSnapshot, PortfolioSnapshot, TechnicalSignal





def _oi_delta_pct(db: Session | None, symbol: str, current_oi: float) -> float | None:

    if db is None or current_oi <= 0:

        return None

    prev = (

        db.query(DerivativesRecord)

        .filter(DerivativesRecord.symbol == symbol)

        .order_by(desc(DerivativesRecord.captured_at))

        .offset(1)

        .first()

    )

    if not prev or not prev.open_interest:

        return None

    return ((current_oi - prev.open_interest) / prev.open_interest) * 100





def build_instrument_opportunities(

    prices: dict[str, float],

    deriv_snapshots: list[DerivativesSnapshot],

    signals: list[TechnicalSignal],

    quant_map: dict[str, int],

    snapshot: PortfolioSnapshot,

    profile: str,

    db: Session | None = None,

) -> str:

    """Structured per-symbol context for perp long/short decisions."""

    risk_config = load_risk_config(profile=profile)
    settings = get_settings()

    deriv_cfg = risk_config.get("derivatives", {})

    whitelist = risk_config["whitelist"]["pairs"]

    quant = QuantEngine()

    ai_cfg = risk_config.get("ai_validation", {})

    min_div_conv = int(ai_cfg.get("min_conviction_on_divergence", 80))

    max_lev = int(deriv_cfg.get("max_leverage", 10))



    deriv_by_symbol = {d.symbol: d for d in deriv_snapshots}

    signal_by_symbol = {s.symbol: s for s in signals}



    lines = [

        f"AI cycle interval: {risk_config.get('ai_cycle_interval_minutes', 30)} min "
        "(discretionary open/close/HOLD only at cycle boundaries).",

        "Risk monitor: automatic SL/TP/liquidation every "
        f"{settings.risk_monitor_interval} min between cycles.",

        f"Available margin (quote balance): {snapshot.quote_balance:.2f} USDT",

        f"Perps ONLY — spot disabled. Max leverage {max_lev}x, max 1 open position.",

        f"ALL-IN: amount_pct=100 on every open and close (full margin / full exit).",

        f"Stop-loss minimum: 100/leverage % (e.g. 10x → 10% SL).",

        f"For perpetuals: amount_pct = margin % of balance; notional = margin × leverage",

        f"Min conviction on quant divergence: {min_div_conv}",

        "",

    ]



    open_perps = [p for p in snapshot.positions if p.instrument_type == "perpetual"]

    if open_perps:

        lines.insert(

            6,

            "⚠ ONE POSITION RULE: position open — only HOLD or close at amount_pct=100.",

        )

    elif snapshot.quote_balance > 0:

        lines.insert(

            6,

            f"⚠ ALL-IN ENTRY: next open must use amount_pct=100 ({snapshot.quote_balance:.2f} USDT margin).",

        )



    for symbol in whitelist:

        spot = prices.get(symbol, 0)

        deriv = deriv_by_symbol.get(symbol)

        sig = signal_by_symbol.get(symbol)

        q_conv = quant_map.get(symbol, 50)



        parts = [f"=== {symbol} ==="]

        if spot > 0:

            parts.append(f"spot={spot:.4f}")

        if deriv:

            basis = None

            if spot > 0 and deriv.mark_price > 0:

                basis = (deriv.mark_price - spot) / spot * 100

            bias = "longs pay" if deriv.funding_rate > 0 else "shorts pay"

            oi_delta = _oi_delta_pct(db, symbol, deriv.open_interest)

            parts.append(

                f"mark={deriv.mark_price:.4f}"

                + (f" basis={basis:+.3f}%" if basis is not None else "")

            )

            parts.append(

                f"funding_8h={deriv.funding_rate_pct:+.4f}% ({bias})"

                + (f" next_funding={deriv.next_funding_time}" if deriv.next_funding_time else "")

            )

            parts.append(f"OI={deriv.open_interest:,.0f}" + (f" delta={oi_delta:+.1f}%" if oi_delta is not None else ""))



        if sig:

            mtf = []

            if sig.trend_1h:

                mtf.append(f"1h={sig.trend_1h}")

            if sig.trend_4h:

                mtf.append(f"4h={sig.trend_4h}")

            if sig.trend_1d:

                mtf.append(f"1d={sig.trend_1d}")

            parts.append(

                f"quant conviction={q_conv} trend={sig.trend}"

                + (f" ({', '.join(mtf)})" if mtf else "")

            )

            stops = quant.suggest_stops_from_atr(sig)

            if stops:

                parts.append(f"suggested SL/TP from ATR: {stops['stop_loss_pct']}% / {stops['take_profit_pct']}%")



        open_pos = [p for p in snapshot.positions if p.symbol == symbol]

        if open_pos:

            for p in open_pos:

                inst = p.instrument_type or "spot"

                side = p.position_side or "long"

                lev = p.leverage or 1

                if inst == "perpetual":

                    margin = p.margin_used or 0

                    notional = p.notional_usdt or 0

                    roe = f"{p.roe_pct:+.1f}%" if p.roe_pct is not None else "N/A"

                    liq = f"{p.liq_distance_pct:.0f}%" if p.liq_distance_pct is not None else "N/A"

                    parts.append(

                        f"OPEN {inst} {side} {lev}x: margin {margin:.2f}, notional {notional:.2f}, "

                        f"entry {p.entry_price:.4f}, mark {p.current_price:.4f}, "

                        f"PnL {p.unrealized_pnl:+.2f}, ROE {roe}, liq_dist ~{liq}"

                    )

        else:

            parts.append("no open position")



        lines.append("\n".join(parts))

        lines.append("")



    return "\n".join(lines).strip()

