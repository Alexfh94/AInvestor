from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from ainvestor.config import get_settings
from ainvestor.db.models import Portfolio, Position, Trade
from ainvestor.models.schemas import (
    PortfolioSnapshot,
    PositionSnapshot,
    TradeSide,
    TradeStatus,
    TradingMode,
)

logger = logging.getLogger(__name__)

DEFAULT_FEE_RATE = 0.001


class PaperTradingSimulator:
    """Internal ledger simulator with real market prices."""

    def __init__(self, db: Session, portfolio: Portfolio):
        self.db = db
        self.portfolio = portfolio

    def execute_buy(
        self,
        symbol: str,
        amount_quote: float,
        price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        cycle_id: str | None = None,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> Trade | None:
        fee = amount_quote * fee_rate
        total_cost = amount_quote + fee

        if self.portfolio.quote_balance < total_cost:
            logger.warning("Insufficient balance for buy %s", symbol)
            return None

        amount_base = amount_quote / price

        self.portfolio.quote_balance -= total_cost

        position = Position(
            portfolio_id=self.portfolio.id,
            symbol=symbol,
            amount=amount_base,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            is_open=True,
        )
        self.db.add(position)

        trade = Trade(
            portfolio_id=self.portfolio.id,
            symbol=symbol,
            side=TradeSide.BUY.value,
            amount=amount_base,
            price=price,
            value_usdt=amount_quote,
            fee=fee,
            status=TradeStatus.EXECUTED.value,
            mode=TradingMode.PAPER.value,
            cycle_id=cycle_id,
        )
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def execute_sell(
        self,
        symbol: str,
        amount_base: float,
        price: float,
        position: Position | None = None,
        cycle_id: str | None = None,
        fee_rate: float = DEFAULT_FEE_RATE,
    ) -> Trade | None:
        if position is None:
            position = self._get_open_position(symbol)
        if position is None or position.amount < amount_base:
            logger.warning("No position or insufficient amount for sell %s", symbol)
            return None

        value_usdt = amount_base * price
        fee = value_usdt * fee_rate
        net_value = value_usdt - fee

        entry_value = amount_base * position.entry_price
        pnl = net_value - entry_value
        self.portfolio.realized_pnl += pnl
        self.portfolio.quote_balance += net_value

        position.amount -= amount_base
        if position.amount <= 1e-10:
            position.is_open = False
            position.closed_at = datetime.utcnow()

        trade = Trade(
            portfolio_id=self.portfolio.id,
            symbol=symbol,
            side=TradeSide.SELL.value,
            amount=amount_base,
            price=price,
            value_usdt=value_usdt,
            fee=fee,
            status=TradeStatus.EXECUTED.value,
            mode=TradingMode.PAPER.value,
            cycle_id=cycle_id,
        )
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def _get_open_position(self, symbol: str) -> Position | None:
        return (
            self.db.query(Position)
            .filter(
                Position.portfolio_id == self.portfolio.id,
                Position.symbol == symbol,
                Position.is_open == True,  # noqa: E712
            )
            .first()
        )

    def get_open_positions(self) -> list[Position]:
        return (
            self.db.query(Position)
            .filter(
                Position.portfolio_id == self.portfolio.id,
                Position.is_open == True,  # noqa: E712
            )
            .all()
        )


class PortfolioManager:
    """Manages portfolio state across paper/testnet/live modes."""

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    def get_or_create_portfolio(self) -> Portfolio:
        portfolio = (
            self.db.query(Portfolio)
            .filter(Portfolio.mode == self.settings.trading_mode)
            .first()
        )
        if portfolio is None:
            portfolio = Portfolio(
                mode=self.settings.trading_mode,
                quote_balance=self.settings.paper_initial_balance,
                quote_currency=self.settings.paper_quote_currency,
            )
            self.db.add(portfolio)
            self.db.commit()
            self.db.refresh(portfolio)
        return portfolio

    def get_simulator(self) -> PaperTradingSimulator:
        return PaperTradingSimulator(self.db, self.get_or_create_portfolio())

    async def get_snapshot(self, prices: dict[str, float]) -> PortfolioSnapshot:
        portfolio = self.get_or_create_portfolio()
        positions = (
            self.db.query(Position)
            .filter(
                Position.portfolio_id == portfolio.id,
                Position.is_open == True,  # noqa: E712
            )
            .all()
        )

        position_snapshots: list[PositionSnapshot] = []
        unrealized_total = 0.0
        positions_value = 0.0

        for pos in positions:
            current = prices.get(pos.symbol, pos.entry_price)
            unrealized = (current - pos.entry_price) * pos.amount
            unrealized_total += unrealized
            pos_value = current * pos.amount
            positions_value += pos_value
            asset = pos.symbol.split("/")[0] if "/" in pos.symbol else pos.symbol
            position_snapshots.append(
                PositionSnapshot(
                    symbol=pos.symbol,
                    asset=asset,
                    amount=pos.amount,
                    entry_price=pos.entry_price,
                    current_price=current,
                    value_usdt=pos_value,
                    pct_of_portfolio=0.0,
                    unrealized_pnl=unrealized,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                )
            )

        total_value = portfolio.quote_balance + positions_value
        invested_usdt = positions_value
        cash_pct = (portfolio.quote_balance / total_value * 100) if total_value > 0 else 100.0

        if total_value > 0:
            for snap in position_snapshots:
                snap.pct_of_portfolio = snap.value_usdt / total_value * 100

        return PortfolioSnapshot(
            mode=TradingMode(portfolio.mode),
            quote_balance=portfolio.quote_balance,
            total_value_usdt=total_value,
            invested_usdt=invested_usdt,
            cash_pct=cash_pct,
            unrealized_pnl=unrealized_total,
            realized_pnl=portfolio.realized_pnl,
            positions=position_snapshots,
            kill_switch_active=portfolio.kill_switch_active,
        )

    def set_kill_switch(self, active: bool) -> None:
        portfolio = self.get_or_create_portfolio()
        portfolio.kill_switch_active = active
        self.db.commit()

    def get_trade_history(self, limit: int = 50) -> list[Trade]:
        portfolio = self.get_or_create_portfolio()
        return (
            self.db.query(Trade)
            .filter(Trade.portfolio_id == portfolio.id)
            .order_by(Trade.executed_at.desc())
            .limit(limit)
            .all()
        )

    def get_initial_value(self) -> float:
        return self.settings.paper_initial_balance

    @staticmethod
    def new_cycle_id() -> str:
        return str(uuid.uuid4())
