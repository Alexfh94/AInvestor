from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import get_settings
from ainvestor.models.schemas import (
    DecisionAction,
    RiskCheckResult,
    TradeProposal,
    TradeStatus,
    TradingMode,
)
from ainvestor.portfolio.manager import PaperTradingSimulator, PortfolioManager

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Executes validated trades in paper, testnet, or live mode."""

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.portfolio_mgr = PortfolioManager(db)

    async def execute_approved(
        self,
        result: RiskCheckResult,
        current_price: float,
        cycle_id: str | None = None,
    ) -> bool:
        if not result.approved or result.proposal is None:
            return False

        proposal = result.proposal
        if proposal.action == DecisionAction.HOLD:
            return True

        mode = TradingMode(self.settings.trading_mode)
        if mode == TradingMode.PAPER:
            return await self._execute_paper(proposal, current_price, cycle_id)
        if mode == TradingMode.TESTNET:
            return await self._execute_testnet(proposal, current_price, cycle_id)
        if mode == TradingMode.LIVE:
            return await self._execute_live(proposal, current_price, cycle_id)
        return False

    async def execute_stop_trigger(
        self, symbol: str, price: float, cycle_id: str | None = None
    ) -> bool:
        simulator = self.portfolio_mgr.get_simulator()
        positions = simulator.get_open_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            return False
        client = ExchangeClient()
        fee_rate = await client.get_taker_fee_rate(symbol)
        trade = simulator.execute_sell(
            symbol, position.amount, price, position, cycle_id, fee_rate=fee_rate
        )
        return trade is not None

    async def _execute_paper(
        self, proposal: TradeProposal, price: float, cycle_id: str | None
    ) -> bool:
        simulator = self.portfolio_mgr.get_simulator()
        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        client = ExchangeClient()
        fee_rate = await client.get_taker_fee_rate(proposal.symbol)

        if proposal.action == DecisionAction.BUY:
            amount_quote = portfolio.quote_balance * (proposal.amount_pct / 100)
            stop_loss = price * (1 - proposal.stop_loss_pct / 100)
            take_profit = price * (1 + proposal.take_profit_pct / 100)
            trade = simulator.execute_buy(
                proposal.symbol,
                amount_quote,
                price,
                stop_loss,
                take_profit,
                cycle_id,
                fee_rate=fee_rate,
            )
            return trade is not None

        if proposal.action == DecisionAction.SELL:
            positions = simulator.get_open_positions()
            position = next((p for p in positions if p.symbol == proposal.symbol), None)
            if position is None:
                return False
            sell_amount = position.amount * (proposal.amount_pct / 100)
            trade = simulator.execute_sell(
                proposal.symbol,
                sell_amount,
                price,
                position,
                cycle_id,
                fee_rate=fee_rate,
            )
            return trade is not None

        return False

    async def _execute_testnet(
        self, proposal: TradeProposal, price: float, cycle_id: str | None
    ) -> bool:
        client = ExchangeClient(exchange_id="binance", testnet=True)
        portfolio = self.portfolio_mgr.get_or_create_portfolio()

        try:
            if proposal.action == DecisionAction.BUY:
                amount_quote = portfolio.quote_balance * (proposal.amount_pct / 100)
                amount_base = amount_quote / price
                order = await client.create_market_order(
                    proposal.symbol, "buy", amount_base
                )
                self._record_exchange_trade(
                    portfolio.id, proposal, order, cycle_id, TradingMode.TESTNET
                )
                return True

            if proposal.action == DecisionAction.SELL:
                simulator = self.portfolio_mgr.get_simulator()
                position = next(
                    (p for p in simulator.get_open_positions() if p.symbol == proposal.symbol),
                    None,
                )
                if position is None:
                    return False
                sell_amount = position.amount * (proposal.amount_pct / 100)
                order = await client.create_market_order(
                    proposal.symbol, "sell", sell_amount
                )
                self._record_exchange_trade(
                    portfolio.id, proposal, order, cycle_id, TradingMode.TESTNET
                )
                return True
        except Exception as e:
            logger.error("Testnet execution failed: %s", e)
            return False
        return False

    async def _execute_live(
        self, proposal: TradeProposal, price: float, cycle_id: str | None
    ) -> bool:
        max_capital = self.settings.live_max_capital_eur
        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        snapshot = await self.portfolio_mgr.get_snapshot({proposal.symbol: price})

        if snapshot.total_value_usdt > max_capital * 1.1:
            logger.error("Live capital exceeds hardcoded limit")
            return False

        client = ExchangeClient(exchange_id="binance", testnet=False)
        try:
            if proposal.action == DecisionAction.BUY:
                amount_quote = min(
                    portfolio.quote_balance * (proposal.amount_pct / 100),
                    max_capital,
                )
                amount_base = amount_quote / price
                order = await client.create_market_order(
                    proposal.symbol, "buy", amount_base
                )
                self._record_exchange_trade(
                    portfolio.id, proposal, order, cycle_id, TradingMode.LIVE
                )
                return True

            if proposal.action == DecisionAction.SELL:
                balance = await client.fetch_balance()
                base = proposal.symbol.split("/")[0]
                available = balance.get(base, {}).get("free", 0)
                sell_amount = available * (proposal.amount_pct / 100)
                if sell_amount <= 0:
                    return False
                order = await client.create_market_order(
                    proposal.symbol, "sell", sell_amount
                )
                self._record_exchange_trade(
                    portfolio.id, proposal, order, cycle_id, TradingMode.LIVE
                )
                return True
        except Exception as e:
            logger.error("Live execution failed: %s", e)
            return False
        return False

    def _record_exchange_trade(
        self,
        portfolio_id: int,
        proposal: TradeProposal,
        order: dict,
        cycle_id: str | None,
        mode: TradingMode,
    ) -> None:
        from ainvestor.db.models import Trade

        trade = Trade(
            portfolio_id=portfolio_id,
            symbol=proposal.symbol,
            side=proposal.action.value,
            amount=order.get("amount", 0),
            price=order.get("average") or order.get("price", 0),
            value_usdt=order.get("cost", 0),
            fee=order.get("fee", {}).get("cost", 0) if order.get("fee") else 0,
            status=TradeStatus.EXECUTED.value,
            mode=mode.value,
            exchange_order_id=order.get("id"),
            cycle_id=cycle_id,
        )
        self.db.add(trade)
        self.db.commit()
