from __future__ import annotations

import logging
import uuid
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy.orm import Session

from ainvestor.config import get_profile_initial_balance, get_settings
from ainvestor.db.models import Portfolio, Position, Trade
from ainvestor.models.schemas import (
    PortfolioSnapshot,
    PositionSnapshot,
    TradeSide,
    TradeStatus,
    TradingMode,
)
from ainvestor.portfolio.profiles import DEFAULT_PROFILE, normalize_profile

logger = logging.getLogger(__name__)

DEFAULT_FEE_RATE = 0.001


def _perp_liq_distance_pct(
    entry: float, current: float, leverage: int, side: str
) -> float | None:
    if leverage <= 1 or entry <= 0:
        return None
    liq_threshold = 100 / leverage * 0.9
    move_pct = abs(current - entry) / entry * 100
    if side == "long":
        if current >= entry:
            return liq_threshold
        return max(0.0, liq_threshold - move_pct)
    if current <= entry:
        return liq_threshold
    return max(0.0, liq_threshold - move_pct)


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
            position.closed_at = app_now()

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
                Position.instrument_type == "spot",
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

    def __init__(self, db: Session, profile: str = DEFAULT_PROFILE):
        self.db = db
        self.settings = get_settings()
        self.profile = normalize_profile(profile)

    def get_or_create_portfolio(self) -> Portfolio:
        portfolio = (
            self.db.query(Portfolio)
            .filter(
                Portfolio.mode == self.settings.trading_mode,
                Portfolio.profile == self.profile,
            )
            .first()
        )
        if portfolio is None:
            initial = get_profile_initial_balance(self.profile)
            portfolio = Portfolio(
                mode=self.settings.trading_mode,
                profile=self.profile,
                quote_balance=initial,
                initial_balance=initial,
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
        positions_equity = 0.0
        invested_usdt = 0.0

        for pos in positions:
            current = prices.get(pos.symbol, pos.entry_price)
            inst = getattr(pos, "instrument_type", "spot") or "spot"
            side = getattr(pos, "position_side", "long") or "long"
            leverage = getattr(pos, "leverage", 1) or 1
            margin = getattr(pos, "margin_used", None) or 0.0

            if inst == "perpetual":
                if side == "long":
                    unrealized = (current - pos.entry_price) * pos.amount
                else:
                    unrealized = (pos.entry_price - current) * pos.amount
                notional = current * pos.amount
                equity = margin + unrealized
                invested_usdt += margin
                roe = (unrealized / margin * 100) if margin > 0 else None
                liq_dist = _perp_liq_distance_pct(pos.entry_price, current, leverage, side)
            else:
                unrealized = (current - pos.entry_price) * pos.amount
                notional = current * pos.amount
                equity = notional
                invested_usdt += notional
                roe = None
                liq_dist = None

            unrealized_total += unrealized
            positions_equity += equity
            asset = pos.symbol.split("/")[0] if "/" in pos.symbol else pos.symbol
            position_snapshots.append(
                PositionSnapshot(
                    symbol=pos.symbol,
                    asset=asset,
                    amount=pos.amount,
                    entry_price=pos.entry_price,
                    current_price=current,
                    value_usdt=equity,
                    pct_of_portfolio=0.0,
                    unrealized_pnl=unrealized,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    instrument_type=inst,
                    position_side=side,
                    leverage=leverage,
                    asset_class=getattr(pos, "asset_class", "crypto"),
                    margin_used=margin if inst == "perpetual" else None,
                    notional_usdt=notional if inst == "perpetual" else None,
                    roe_pct=roe,
                    liq_distance_pct=liq_dist,
                )
            )

        total_value = portfolio.quote_balance + positions_equity
        cash_pct = (portfolio.quote_balance / total_value * 100) if total_value > 0 else 100.0

        if total_value > 0:
            for snap in position_snapshots:
                snap.pct_of_portfolio = snap.value_usdt / total_value * 100

        return PortfolioSnapshot(
            mode=TradingMode(portfolio.mode),
            profile=portfolio.profile,
            portfolio_id=portfolio.id,
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
        portfolio = self.get_or_create_portfolio()
        return portfolio.initial_balance or get_profile_initial_balance(self.profile)

    @staticmethod
    def new_cycle_id() -> str:
        return str(uuid.uuid4())
