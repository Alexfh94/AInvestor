from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy.orm import Session

from ainvestor.config import load_risk_config
from ainvestor.db.models import Portfolio, Position, Trade
from ainvestor.models.schemas import TradeSide, TradeStatus, TradingMode
from ainvestor.portfolio.perp_sizing import compute_all_in_perp_open

logger = logging.getLogger(__name__)

FUNDING_INTERVAL_HOURS = 8


class PerpPaperSimulator:
    """Paper simulator for crypto perpetuals with margin and funding."""

    def __init__(self, db: Session, portfolio: Portfolio):
        self.db = db
        self.portfolio = portfolio
        profile = getattr(portfolio, "profile", None) or "extreme"
        self.config = load_risk_config(profile=profile).get("derivatives", {})

    def open_position(
        self,
        symbol: str,
        side: str,
        notional_usdt: float,
        price: float,
        leverage: int,
        stop_loss: float | None,
        take_profit: float | None,
        cycle_id: str | None = None,
        fee_rate: float = 0.001,
        margin_used: float | None = None,
        opening_fee: float | None = None,
    ) -> Trade | None:
        leverage = min(leverage, int(self.config.get("max_leverage", 2)))

        if margin_used is not None and opening_fee is not None:
            margin = margin_used
            fee = opening_fee
            total_required = margin + fee
        else:
            margin = notional_usdt / leverage
            fee = notional_usdt * fee_rate
            total_required = margin + fee
            # Float-safe all-in: fee absorbs rounding so total never exceeds balance
            balance = self.portfolio.quote_balance
            if total_required > balance and (total_required - balance) <= 0.05:
                fee = balance - margin
                total_required = balance

        if self.portfolio.quote_balance + 1e-9 < total_required:
            logger.warning(
                "Insufficient margin for perp %s (need %.6f, have %.6f)",
                symbol,
                total_required,
                self.portfolio.quote_balance,
            )
            return None

        amount_base = notional_usdt / price
        self.portfolio.quote_balance -= total_required

        position = Position(
            portfolio_id=self.portfolio.id,
            symbol=symbol,
            amount=amount_base,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            instrument_type="perpetual",
            position_side=side,
            leverage=leverage,
            margin_used=margin,
            asset_class="derivative",
            last_funding_at=app_now(),
            is_open=True,
        )
        self.db.add(position)

        trade_side = TradeSide.BUY.value if side == "long" else TradeSide.SELL.value
        trade = Trade(
            portfolio_id=self.portfolio.id,
            symbol=symbol,
            side=trade_side,
            amount=amount_base,
            price=price,
            value_usdt=notional_usdt,
            fee=fee,
            status=TradeStatus.EXECUTED.value,
            mode=TradingMode.PAPER.value,
            instrument_type="perpetual",
            position_side=side,
            leverage=leverage,
            asset_class="derivative",
            cycle_id=cycle_id,
            trade_action="open",
        )
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def close_position(
        self,
        position: Position,
        price: float,
        close_pct: float = 100.0,
        cycle_id: str | None = None,
        fee_rate: float = 0.001,
    ) -> Trade | None:
        close_amount = position.amount * (close_pct / 100)
        if close_amount <= 0:
            return None

        notional = close_amount * price
        fee = notional * fee_rate

        if position.position_side == "long":
            pnl = (price - position.entry_price) * close_amount
        else:
            pnl = (position.entry_price - price) * close_amount

        margin_release = (position.margin_used or 0) * (close_pct / 100)
        net_pnl = pnl - fee
        net = margin_release + pnl - fee
        self.portfolio.quote_balance += net
        self.portfolio.realized_pnl += net_pnl
        pnl_pct_roe = (net_pnl / margin_release * 100) if margin_release > 0 else None

        position.amount -= close_amount
        if position.margin_used:
            position.margin_used *= 1 - close_pct / 100
        if position.amount <= 1e-10:
            position.is_open = False
            position.closed_at = app_now()

        trade = Trade(
            portfolio_id=self.portfolio.id,
            symbol=position.symbol,
            side=TradeSide.SELL.value if position.position_side == "long" else TradeSide.BUY.value,
            amount=close_amount,
            price=price,
            value_usdt=notional,
            fee=fee,
            status=TradeStatus.EXECUTED.value,
            mode=TradingMode.PAPER.value,
            instrument_type="perpetual",
            position_side=position.position_side,
            leverage=position.leverage,
            asset_class="derivative",
            cycle_id=cycle_id,
            trade_action="close",
            realized_pnl_usdt=round(net_pnl, 6),
            pnl_pct_roe=round(pnl_pct_roe, 4) if pnl_pct_roe is not None else None,
        )
        self.db.add(trade)
        self.db.commit()
        return trade

    def check_liquidation(self, position: Position, price: float) -> bool:
        if position.leverage <= 1:
            return False
        move_pct = abs(price - position.entry_price) / position.entry_price * 100
        liq_threshold = 100 / position.leverage * 0.9
        if position.position_side == "long" and price < position.entry_price:
            return move_pct >= liq_threshold
        if position.position_side == "short" and price > position.entry_price:
            return move_pct >= liq_threshold
        return False

    def apply_funding(self, position: Position, funding_rate: float) -> float:
        """Apply 8h funding payment (positive rate = longs pay shorts)."""
        notional = position.amount * position.entry_price
        payment = notional * funding_rate
        if position.position_side == "long":
            cost = payment if funding_rate > 0 else -payment
        else:
            cost = -payment if funding_rate > 0 else payment
        self.portfolio.quote_balance -= cost
        position.last_funding_at = app_now()
        self.db.commit()
        return cost
