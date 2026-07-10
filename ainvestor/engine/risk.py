from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy import func
from sqlalchemy.orm import Session

from ainvestor.config import get_settings, load_risk_config
from ainvestor.db.models import RiskEvent, Trade, Position
from ainvestor.models.schemas import (
    AssetClass,
    DecisionAction,
    InstrumentType,
    PortfolioSnapshot,
    RiskCheckResult,
    TradeProposal,
    TradeSide,
)
from ainvestor.services.market_hours import is_us_market_open

logger = logging.getLogger(__name__)


def max_position_pct_for_conviction(conviction: int, config: dict | None = None) -> float:
    cfg = config or load_risk_config()
    pos_cfg = cfg["position"]
    base = float(pos_cfg["max_position_pct"])
    high = float(pos_cfg.get("max_position_pct_high_conviction", base))
    threshold = int(pos_cfg.get("high_conviction_threshold", 70))
    conviction = max(0, min(100, conviction))
    if conviction >= threshold:
        span = max(1, 100 - threshold)
        ratio = (conviction - threshold) / span
        return base + ratio * (high - base)
    if threshold <= 0:
        return base
    return base * (conviction / threshold)


class RiskManager:
    """Deterministic risk gate — AI proposals must pass all checks."""

    def __init__(self, db: Session):
        self.db = db
        self.config = load_risk_config()
        self.settings = get_settings()

    def max_position_pct_for_conviction(self, conviction: int) -> float:
        return max_position_pct_for_conviction(conviction, self.config)

    def validate_proposal(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        current_price: float,
        cycle_id: str | None = None,
        fee_rate: float = 0.001,
        quant_conviction: int | None = None,
    ) -> RiskCheckResult:
        reasons: list[str] = []

        if portfolio.kill_switch_active:
            reasons.append("Kill switch is active")

        if self._global_kill_switch_active():
            reasons.append("Global kill switch active (risk.yaml)")

        if proposal.action == DecisionAction.HOLD:
            return RiskCheckResult(approved=True, proposal=proposal)

        reasons.extend(self._check_live_mode_allowed(proposal))
        reasons.extend(self._check_conviction_divergence(proposal, quant_conviction))

        if proposal.instrument_type == InstrumentType.PERPETUAL:
            reasons.extend(self._validate_perp(proposal, portfolio, current_price, fee_rate))
        elif proposal.instrument_type == InstrumentType.STOCK or proposal.asset_class == AssetClass.STOCK:
            reasons.extend(self._validate_stock(proposal, portfolio, current_price))
        else:
            reasons.extend(self._validate_spot(proposal, portfolio, current_price, fee_rate, cycle_id))

        if reasons:
            self._log_rejection(proposal, reasons, cycle_id)
            return RiskCheckResult(approved=False, proposal=proposal, rejection_reasons=reasons)

        return RiskCheckResult(approved=True, proposal=proposal)

    def _validate_spot(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        current_price: float,
        fee_rate: float,
        cycle_id: str | None,
    ) -> list[str]:
        reasons: list[str] = []
        whitelist = self.config["whitelist"]["pairs"]
        if proposal.symbol not in whitelist:
            reasons.append(f"Symbol {proposal.symbol} not in whitelist")

        if proposal.action == DecisionAction.BUY:
            reasons.extend(self._validate_buy(proposal, portfolio, current_price, fee_rate))
        elif proposal.action == DecisionAction.SELL:
            reasons.extend(self._validate_sell(proposal, portfolio, fee_rate))
        return reasons

    def _validate_perp(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        current_price: float,
        fee_rate: float,
    ) -> list[str]:
        reasons: list[str] = []
        deriv = self.config.get("derivatives", {})
        if not deriv.get("enabled", False):
            reasons.append("Derivatives disabled in risk config")
        if deriv.get("paper_only", True) and self.settings.trading_mode == "live":
            live_cfg = self.config.get("modes", {}).get("live", {})
            if not live_cfg.get("gradual_activation", {}).get("crypto_perps", False):
                reasons.append("Live perps not activated (gradual_activation.crypto_perps=false)")

        max_lev = int(deriv.get("max_leverage", 2))
        mifid_cap = int(deriv.get("mifid_retail_leverage_cap", 2))
        cap = min(max_lev, mifid_cap)
        if proposal.leverage > cap:
            reasons.append(f"Leverage {proposal.leverage}x exceeds cap {cap}x")

        open_perps = (
            self.db.query(func.count(Position.id))
            .filter(
                Position.portfolio_id == self._portfolio_id(portfolio),
                Position.is_open == True,  # noqa: E712
                Position.instrument_type == "perpetual",
            )
            .scalar()
        ) or 0
        if open_perps >= int(deriv.get("max_open_perps", 2)):
            reasons.append("Max open perpetual positions reached")

        if proposal.action == DecisionAction.BUY and proposal.position_side == "long":
            reasons.extend(self._validate_buy(proposal, portfolio, current_price, fee_rate))
        elif proposal.action in (DecisionAction.SELL, DecisionAction.BUY):
            if proposal.position_side == "short" and proposal.action == DecisionAction.SELL:
                reasons.extend(self._validate_buy(proposal, portfolio, current_price, fee_rate))
            else:
                reasons.extend(self._validate_sell(proposal, portfolio, fee_rate))
        return reasons

    def _validate_stock(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        current_price: float,
    ) -> list[str]:
        reasons: list[str] = []
        stocks_cfg = self.config.get("stocks", {})
        if not stocks_cfg.get("enabled", False):
            reasons.append("Stocks disabled in risk config")

        whitelist = self.config.get("assets", {}).get("stocks", [])
        if proposal.symbol not in whitelist:
            reasons.append(f"Stock {proposal.symbol} not in whitelist")

        if stocks_cfg.get("market_hours_required", True) and not is_us_market_open():
            reasons.append("US market closed — no stock trades")

        if stocks_cfg.get("paper_only", True) and self.settings.trading_mode == "live":
            live_cfg = self.config.get("modes", {}).get("live", {})
            if not live_cfg.get("gradual_activation", {}).get("stocks", False):
                reasons.append("Live stocks not activated")

        if proposal.position_side == "short":
            reasons.append("Stock short not supported in paper mode")

        if proposal.action == DecisionAction.BUY:
            order_value = portfolio.quote_balance * (proposal.amount_pct / 100)
            min_eur = float(stocks_cfg.get("min_order_value_eur", 10))
            if order_value < min_eur:
                reasons.append(f"Stock order below minimum {min_eur} EUR equivalent")
        return reasons

    def _check_conviction_divergence(
        self, proposal: TradeProposal, quant_conviction: int | None
    ) -> list[str]:
        if quant_conviction is None:
            return []
        ai_cfg = self.config.get("ai_validation", {})
        threshold = int(ai_cfg.get("conviction_divergence_threshold", 30))
        min_conv = int(ai_cfg.get("min_conviction_on_divergence", 80))
        divergence = abs(proposal.conviction - quant_conviction)
        if divergence > threshold and proposal.conviction < min_conv:
            return [
                f"IA conviction {proposal.conviction} diverges {divergence}pts from "
                f"quant {quant_conviction} — requires conviction >= {min_conv}"
            ]
        return []

    def _check_live_mode_allowed(self, proposal: TradeProposal) -> list[str]:
        if self.settings.trading_mode != "live":
            return []
        live_cfg = self.config.get("modes", {}).get("live", {})
        if not live_cfg.get("enabled", False):
            return ["Live mode disabled in risk.yaml"]

        activation = live_cfg.get("gradual_activation", {})
        caps = live_cfg.get("caps_per_class_eur", {})

        if proposal.instrument_type == InstrumentType.SPOT:
            if not activation.get("crypto_spot", False):
                return ["Live crypto spot not activated"]
            cap = float(caps.get("crypto", live_cfg.get("max_capital_eur", 100)))
            if self.settings.live_max_crypto_eur > cap:
                return [f"Live crypto cap {self.settings.live_max_crypto_eur} exceeds config {cap}"]
        return []

    def _global_kill_switch_active(self) -> bool:
        live_cfg = self.config.get("modes", {}).get("live", {})
        return bool(live_cfg.get("kill_switch_global")) and self.settings.trading_mode == "live"

    def _validate_buy(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        current_price: float,
        fee_rate: float,
    ) -> list[str]:
        reasons: list[str] = []
        pos_cfg = self.config["position"]
        stops_cfg = self.config["stops"]
        limits_cfg = self.config["limits"]

        open_positions = len(portfolio.positions)
        if open_positions >= pos_cfg["max_open_positions"]:
            reasons.append(f"Max open positions ({pos_cfg['max_open_positions']}) reached")

        order_value = portfolio.quote_balance * (proposal.amount_pct / 100)
        total_cost = order_value * (1 + fee_rate)

        if order_value < pos_cfg["min_order_value_usdt"]:
            reasons.append(f"Order value {order_value:.2f} below minimum")

        if total_cost > portfolio.quote_balance:
            reasons.append("Insufficient balance including fee")

        position_pct = (
            (order_value / portfolio.total_value_usdt) * 100 if portfolio.total_value_usdt else 0
        )
        max_allowed = self.max_position_pct_for_conviction(proposal.conviction)
        if position_pct > max_allowed:
            reasons.append(
                f"Position size {position_pct:.1f}% exceeds max {max_allowed:.1f}% "
                f"for conviction {proposal.conviction}"
            )

        if stops_cfg["require_stop_loss"] and proposal.stop_loss_pct <= 0:
            reasons.append("Stop-loss is required")
        if stops_cfg["require_take_profit"] and proposal.take_profit_pct <= 0:
            reasons.append("Take-profit is required")

        if proposal.stop_loss_pct > stops_cfg["max_stop_loss_pct"]:
            reasons.append(f"Stop-loss exceeds max {stops_cfg['max_stop_loss_pct']}%")

        if proposal.take_profit_pct < stops_cfg["min_take_profit_pct"]:
            reasons.append("Take-profit below minimum")

        min_net_gain_pct = (fee_rate * 2) * 100 + 0.5
        if proposal.take_profit_pct < min_net_gain_pct:
            reasons.append("Take-profit too low to cover round-trip fees")

        reasons.extend(self._check_loss_limits(portfolio, limits_cfg))
        reasons.extend(self._check_trade_count_limits(limits_cfg))
        return reasons

    def _validate_sell(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        fee_rate: float,
    ) -> list[str]:
        reasons: list[str] = []
        position = next((p for p in portfolio.positions if p.symbol == proposal.symbol), None)
        if position is None:
            reasons.append(f"No open position for {proposal.symbol}")
            return reasons

        sell_value = position.amount * (proposal.amount_pct / 100) * position.current_price
        fee = sell_value * fee_rate
        if sell_value - fee <= 0:
            reasons.append("Sell value too small after fees")
        return reasons

    def _check_loss_limits(self, portfolio: PortfolioSnapshot, limits_cfg: dict) -> list[str]:
        reasons: list[str] = []
        initial = self.settings.paper_initial_balance
        if initial <= 0:
            return reasons

        drawdown_pct = ((initial - portfolio.total_value_usdt) / initial) * 100
        if drawdown_pct >= limits_cfg["max_drawdown_pct"]:
            reasons.append(f"Max drawdown {drawdown_pct:.1f}% exceeded")

        daily_loss = self._get_period_loss_pct(days=1)
        if daily_loss >= limits_cfg["max_daily_loss_pct"]:
            reasons.append(f"Daily loss limit {daily_loss:.1f}% exceeded")

        weekly_loss = self._get_period_loss_pct(days=7)
        if weekly_loss >= limits_cfg["max_weekly_loss_pct"]:
            reasons.append(f"Weekly loss limit {weekly_loss:.1f}% exceeded")

        return reasons

    def _check_trade_count_limits(self, limits_cfg: dict) -> list[str]:
        today = app_now().replace(hour=0, minute=0, second=0, microsecond=0)
        count = (
            self.db.query(func.count(Trade.id)).filter(Trade.executed_at >= today).scalar()
        ) or 0
        if count >= limits_cfg["max_trades_per_day"]:
            return [f"Max trades per day ({limits_cfg['max_trades_per_day']}) reached"]
        return []

    def _get_period_loss_pct(self, days: int) -> float:
        since = app_now() - timedelta(days=days)
        sells = (
            self.db.query(Trade)
            .filter(Trade.executed_at >= since, Trade.side == TradeSide.SELL.value)
            .all()
        )
        if not sells:
            return 0.0
        total_pnl = sum(t.value_usdt - t.fee for t in sells)
        initial = self.settings.paper_initial_balance
        if total_pnl >= 0:
            return 0.0
        return abs(total_pnl / initial) * 100

    def check_stop_loss_take_profit(
        self, portfolio: PortfolioSnapshot
    ) -> list[tuple[str, str, float]]:
        triggers: list[tuple[str, str, float]] = []
        for pos in portfolio.positions:
            if pos.stop_loss and pos.current_price <= pos.stop_loss:
                triggers.append((pos.symbol, "sell", pos.current_price))
            elif pos.take_profit and pos.current_price >= pos.take_profit:
                triggers.append((pos.symbol, "sell", pos.current_price))
        return triggers

    def should_activate_kill_switch(self, portfolio: PortfolioSnapshot) -> bool:
        initial = self.settings.paper_initial_balance
        if initial <= 0:
            return False
        drawdown = ((initial - portfolio.total_value_usdt) / initial) * 100
        return drawdown >= self.config["limits"]["max_drawdown_pct"]

    def _portfolio_id(self, portfolio: PortfolioSnapshot) -> int:
        from ainvestor.portfolio.manager import PortfolioManager

        return PortfolioManager(self.db).get_or_create_portfolio().id

    def _log_rejection(
        self, proposal: TradeProposal, reasons: list[str], cycle_id: str | None
    ) -> None:
        event = RiskEvent(
            event_type="proposal_rejected",
            symbol=proposal.symbol,
            details="; ".join(reasons),
            cycle_id=cycle_id,
        )
        self.db.add(event)
        self.db.commit()
        logger.info("Rejected %s %s: %s", proposal.action, proposal.symbol, reasons)
