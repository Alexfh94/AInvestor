from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy.orm import Session

from ainvestor.config import load_risk_config
from ainvestor.db.models import Portfolio, Position, Trade
from ainvestor.models.schemas import TradeSide, TradeStatus, TradingMode

logger = logging.getLogger(__name__)

FUNDING_INTERVAL_HOURS = 8


class PerpPaperSimulator:
    """Paper simulator for crypto perpetuals with margin and funding."""

    def __init__(self, db: Session, portfolio: Portfolio):
        self.db = db
        self.portfolio = portfolio
        self.config = load_risk_config().get("derivatives", {})

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
    ) -> Trade | None:
        leverage = min(leverage, int(self.config.get("max_leverage", 2)))
        margin = notional_usdt / leverage
        fee = notional_usdt * fee_rate
        total_required = margin + fee

        if self.portfolio.quote_balance < total_required:
            logger.warning("Insufficient margin for perp %s", symbol)
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
        net = margin_release + pnl - fee
        self.portfolio.quote_balance += net
        self.portfolio.realized_pnl += pnl - fee

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
        return cost
