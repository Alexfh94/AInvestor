from __future__ import annotations

"""Perpetual position sizing helpers (all-in margin + fee)."""


def compute_all_in_perp_open(
    quote_balance: float,
    leverage: int,
    fee_rate: float,
    fee_reserve_pct: float = 0.0,
) -> tuple[float, float, float]:
    """
    Size an all-in perp open so margin + opening fee uses available balance.

    fee_reserve_pct: tiny cash buffer left for funding/rounding (e.g. 0.1 = 0.1%).

    Returns (margin, notional, fee) with margin + fee <= quote_balance.
    """
    if leverage <= 0 or quote_balance <= 0:
        return 0.0, 0.0, 0.0
    reserve = max(0.0, min(fee_reserve_pct, 5.0))
    usable = quote_balance * (1 - reserve / 100)
    margin = usable / (1 + leverage * fee_rate)
    notional = margin * leverage
    fee = usable - margin
    return margin, notional, fee
