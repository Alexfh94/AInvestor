from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy.orm import Session

from ainvestor.config import get_settings, load_risk_config
from ainvestor.db.models import StockPortfolio, StockPosition, Trade
from ainvestor.models.schemas import TradeSide, TradeStatus, TradingMode

logger = logging.getLogger(__name__)


class StockPaperSimulator:
    """Paper simulator for US stocks/ETFs."""

    def __init__(self, db: Session, portfolio: StockPortfolio):
        self.db = db
        self.portfolio = portfolio
        self.config = load_risk_config().get("stocks", {})

    def execute_buy(
        self,
        symbol: str,
        amount_usd: float,
        price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        cycle_id: str | None = None,
        fee_rate: float = 0.0,
    ) -> Trade | None:
        fee = amount_usd * fee_rate
        total = amount_usd + fee
        if self.portfolio.cash_usd < total:
            logger.warning("Insufficient USD for stock buy %s", symbol)
            return None

        shares = amount_usd / price
        self.portfolio.cash_usd -= total

        position = StockPosition(
            portfolio_id=self.portfolio.id,
            symbol=symbol,
            shares=shares,
            entry_price=price,
            currency="USD",
            stop_loss=stop_loss,
            take_profit=take_profit,
            is_open=True,
        )
        self.db.add(position)

        trade = Trade(
            portfolio_id=0,
            symbol=symbol,
            side=TradeSide.BUY.value,
            amount=shares,
            price=price,
            value_usdt=amount_usd,
            fee=fee,
            status=TradeStatus.EXECUTED.value,
            mode=TradingMode.PAPER.value,
            instrument_type="stock",
            asset_class="stock",
            cycle_id=cycle_id,
        )
        self.db.add(trade)
        self.db.commit()
        return trade

    def execute_sell(
        self,
        symbol: str,
        shares: float,
        price: float,
        position: StockPosition | None = None,
        cycle_id: str | None = None,
        fee_rate: float = 0.0,
    ) -> Trade | None:
        if position is None:
            position = self._get_open(symbol)
        if position is None or position.shares < shares:
            return None

        value = shares * price
        fee = value * fee_rate
        pnl = (price - position.entry_price) * shares - fee
        self.portfolio.cash_usd += value - fee
        self.portfolio.realized_pnl_eur += pnl

        position.shares -= shares
        if position.shares <= 1e-8:
            position.is_open = False
            position.closed_at = app_now()

        trade = Trade(
            portfolio_id=0,
            symbol=symbol,
            side=TradeSide.SELL.value,
            amount=shares,
            price=price,
            value_usdt=value,
            fee=fee,
            status=TradeStatus.EXECUTED.value,
            mode=TradingMode.PAPER.value,
            instrument_type="stock",
            asset_class="stock",
            cycle_id=cycle_id,
        )
        self.db.add(trade)
        self.db.commit()
        return trade

    def _get_open(self, symbol: str) -> StockPosition | None:
        return (
            self.db.query(StockPosition)
            .filter(
                StockPosition.portfolio_id == self.portfolio.id,
                StockPosition.symbol == symbol,
                StockPosition.is_open == True,  # noqa: E712
            )
            .first()
        )

    def get_open_positions(self) -> list[StockPosition]:
        return (
            self.db.query(StockPosition)
            .filter(
                StockPosition.portfolio_id == self.portfolio.id,
                StockPosition.is_open == True,  # noqa: E712
            )
            .all()
        )


class StockPortfolioManager:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    def get_or_create(self) -> StockPortfolio:
        portfolio = self.db.query(StockPortfolio).filter(StockPortfolio.mode == "paper").first()
        if portfolio is None:
            stocks_cfg = load_risk_config().get("stocks", {})
            initial_eur = float(load_risk_config().get("modes", {}).get("live", {}).get("caps_per_class_eur", {}).get("stocks", 0) or 0)
            portfolio = StockPortfolio(
                mode="paper",
                cash_eur=initial_eur if initial_eur > 0 else 0,
                cash_usd=self.settings.paper_initial_balance * 0.5,
            )
            self.db.add(portfolio)
            self.db.commit()
            self.db.refresh(portfolio)
        return portfolio

    def get_simulator(self) -> StockPaperSimulator:
        return StockPaperSimulator(self.db, self.get_or_create())
