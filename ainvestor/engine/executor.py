from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import get_settings, load_risk_config
from ainvestor.models.schemas import (
    AssetClass,
    DecisionAction,
    InstrumentType,
    RiskCheckResult,
    TradeProposal,
    TradeStatus,
    TradingMode,
)
from ainvestor.portfolio.manager import PaperTradingSimulator, PortfolioManager
from ainvestor.portfolio.perp_simulator import PerpPaperSimulator
from ainvestor.portfolio.perp_sizing import compute_all_in_perp_open
from ainvestor.portfolio.profiles import DEFAULT_PROFILE, PROFILE_EXTREME, normalize_profile
from ainvestor.portfolio.stock_simulator import StockPortfolioManager
from ainvestor.services.market_hours import is_us_market_open

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Routes validated trades to spot, perp, stock or IBKR adapters."""

    def __init__(self, db: Session, profile: str = DEFAULT_PROFILE):
        self.db = db
        self.settings = get_settings()
        self.profile = normalize_profile(profile)
        self.portfolio_mgr = PortfolioManager(db, profile=self.profile)
        self.stock_mgr = StockPortfolioManager(db)

    async def execute_approved(
        self,
        result: RiskCheckResult,
        current_price: float,
        cycle_id: str | None = None,
        funding_rate: float = 0.0,
    ) -> bool:
        if not result.approved or result.proposal is None:
            return False

        proposal = result.proposal
        if proposal.action == DecisionAction.HOLD:
            return True

        if proposal.instrument_type == InstrumentType.PERPETUAL:
            return await self._execute_perp_paper(proposal, current_price, cycle_id, funding_rate)

        if proposal.instrument_type == InstrumentType.STOCK or proposal.asset_class == AssetClass.STOCK:
            return await self._execute_stock(proposal, current_price, cycle_id)

        mode = TradingMode(self.settings.trading_mode)
        if mode == TradingMode.PAPER:
            return await self._execute_paper(proposal, current_price, cycle_id)
        if mode == TradingMode.TESTNET:
            return await self._execute_testnet(proposal, current_price, cycle_id)
        if mode == TradingMode.LIVE:
            return await self._execute_live(proposal, current_price, cycle_id)
        return False

    async def _execute_perp_paper(
        self,
        proposal: TradeProposal,
        price: float,
        cycle_id: str | None,
        funding_rate: float,
    ) -> bool:
        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        simulator = PerpPaperSimulator(self.db, portfolio)
        client = ExchangeClient()
        fee_rate = await client.get_taker_fee_rate(proposal.symbol)

        margin: float | None = None
        opening_fee: float | None = None
        if self.profile == PROFILE_EXTREME:
            reserve = float(
                load_risk_config(profile=self.profile)
                .get("fees", {})
                .get("all_in_reserve_pct", 0.1)
            )
            margin, notional, opening_fee = compute_all_in_perp_open(
                portfolio.quote_balance, proposal.leverage, fee_rate, fee_reserve_pct=reserve
            )
        else:
            margin = portfolio.quote_balance * (proposal.amount_pct / 100)
            notional = margin * proposal.leverage
        close_pct = 100.0 if self.profile == PROFILE_EXTREME else proposal.amount_pct

        def _stops_for_side() -> tuple[float, float]:
            if proposal.position_side == "short":
                stop = price * (1 + proposal.stop_loss_pct / 100)
                tp = price * (1 - proposal.take_profit_pct / 100)
            else:
                stop = price * (1 - proposal.stop_loss_pct / 100)
                tp = price * (1 + proposal.take_profit_pct / 100)
            return stop, tp

        positions = self.portfolio_mgr.get_simulator().get_open_positions()
        pos = next(
            (
                p
                for p in positions
                if p.symbol == proposal.symbol
                and p.instrument_type == "perpetual"
                and p.is_open
            ),
            None,
        )

        if proposal.action == DecisionAction.BUY and proposal.position_side == "long":
            if pos is not None:
                return False
            stop, tp = _stops_for_side()
            trade = simulator.open_position(
                proposal.symbol,
                "long",
                notional,
                price,
                proposal.leverage,
                stop,
                tp,
                cycle_id,
                fee_rate,
                margin_used=margin,
                opening_fee=opening_fee,
            )
            if trade and funding_rate:
                positions = self.portfolio_mgr.get_simulator().get_open_positions()
                new_pos = next(
                    (p for p in positions if p.symbol == proposal.symbol and p.is_open),
                    None,
                )
                if new_pos:
                    simulator.apply_funding(new_pos, funding_rate)
            return trade is not None

        if proposal.action == DecisionAction.SELL and proposal.position_side == "short":
            if pos is None:
                stop, tp = _stops_for_side()
                trade = simulator.open_position(
                    proposal.symbol,
                    "short",
                    notional,
                    price,
                    proposal.leverage,
                    stop,
                    tp,
                    cycle_id,
                    fee_rate,
                    margin_used=margin,
                    opening_fee=opening_fee,
                )
                if trade and funding_rate:
                    positions = self.portfolio_mgr.get_simulator().get_open_positions()
                    new_pos = next(
                        (p for p in positions if p.symbol == proposal.symbol and p.is_open),
                        None,
                    )
                    if new_pos:
                        simulator.apply_funding(new_pos, funding_rate)
                return trade is not None

        if proposal.action == DecisionAction.SELL and pos is not None:
            trade = simulator.close_position(pos, price, close_pct, cycle_id, fee_rate)
            return trade is not None

        if proposal.action == DecisionAction.BUY and pos is not None and proposal.position_side == "short":
            trade = simulator.close_position(pos, price, close_pct, cycle_id, fee_rate)
            return trade is not None

        return False

    async def _execute_stock(self, proposal: TradeProposal, price: float, cycle_id: str | None) -> bool:
        if self.settings.stock_trading_mode.startswith("ibkr"):
            return await self._execute_stock_ibkr(proposal, price, cycle_id)

        if not is_us_market_open() and load_risk_config().get("stocks", {}).get("market_hours_required", True):
            logger.warning("Stock trade blocked — market closed")
            return False

        simulator = self.stock_mgr.get_simulator()
        stock_portfolio = self.stock_mgr.get_or_create()

        if proposal.action == DecisionAction.BUY:
            amount_usd = stock_portfolio.cash_usd * (proposal.amount_pct / 100)
            stop = price * (1 - proposal.stop_loss_pct / 100)
            tp = price * (1 + proposal.take_profit_pct / 100)
            trade = simulator.execute_buy(
                proposal.symbol, amount_usd, price, stop, tp, cycle_id
            )
            return trade is not None

        if proposal.action == DecisionAction.SELL:
            pos = next(
                (p for p in simulator.get_open_positions() if p.symbol == proposal.symbol),
                None,
            )
            if pos is None:
                return False
            shares = pos.shares * (proposal.amount_pct / 100)
            trade = simulator.execute_sell(proposal.symbol, shares, price, pos, cycle_id)
            return trade is not None
        return False

    async def _execute_stock_ibkr(
        self, proposal: TradeProposal, price: float, cycle_id: str | None
    ) -> bool:
        from ainvestor.brokers.ibkr import IBKRBroker

        broker = IBKRBroker()
        if not await broker.connect():
            return False
        try:
            stock_portfolio = self.stock_mgr.get_or_create()
            if proposal.action == DecisionAction.BUY:
                amount_usd = stock_portfolio.cash_usd * (proposal.amount_pct / 100)
                qty = amount_usd / price
                result = await broker.place_market_order(proposal.symbol, "buy", qty)
                return result is not None
            if proposal.action == DecisionAction.SELL:
                pos = next(
                    (p for p in self.stock_mgr.get_simulator().get_open_positions() if p.symbol == proposal.symbol),
                    None,
                )
                if pos is None:
                    return False
                qty = pos.shares * (proposal.amount_pct / 100)
                result = await broker.place_market_order(proposal.symbol, "sell", qty)
                return result is not None
        finally:
            await broker.disconnect()
        return False

    async def execute_stop_trigger(
        self, symbol: str, price: float, cycle_id: str | None = None
    ) -> bool:
        simulator = self.portfolio_mgr.get_simulator()
        positions = simulator.get_open_positions()
        position = next(
            (
                p
                for p in positions
                if p.symbol == symbol
                and (getattr(p, "instrument_type", "spot") == "perpetual" or p.instrument_type == "spot")
            ),
            None,
        )
        if position is None:
            position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            return False

        if getattr(position, "instrument_type", "spot") == "perpetual":
            perp_sim = PerpPaperSimulator(self.db, self.portfolio_mgr.get_or_create_portfolio())
            trade = perp_sim.close_position(position, price, 100.0, cycle_id)
            return trade is not None

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
        live_cfg = load_risk_config().get("modes", {}).get("live", {})
        if not live_cfg.get("enabled", False):
            logger.error("Live mode disabled in risk.yaml")
            return False

        activation = live_cfg.get("gradual_activation", {})
        if proposal.instrument_type == InstrumentType.SPOT and not activation.get("crypto_spot", False):
            logger.error("Live crypto spot not activated")
            return False

        max_capital = min(
            self.settings.live_max_capital_eur,
            float(live_cfg.get("max_capital_eur", 100)),
        )
        caps = live_cfg.get("caps_per_class_eur", {})
        class_cap = float(caps.get("crypto", max_capital))

        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        snapshot = await self.portfolio_mgr.get_snapshot({proposal.symbol: price})

        if snapshot.total_value_usdt > class_cap * 1.1:
            logger.error("Live capital exceeds class cap")
            return False

        client = ExchangeClient(exchange_id="binance", testnet=False)
        try:
            if proposal.action == DecisionAction.BUY:
                amount_quote = min(
                    portfolio.quote_balance * (proposal.amount_pct / 100),
                    class_cap,
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
            instrument_type=proposal.instrument_type.value,
            position_side=proposal.position_side,
            leverage=proposal.leverage,
            asset_class=proposal.asset_class.value,
            exchange_order_id=order.get("id"),
            cycle_id=cycle_id,
        )
        self.db.add(trade)
        self.db.commit()
