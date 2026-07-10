from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ainvestor.config import get_settings, load_risk_config
from ainvestor.db.models import RiskEvent, Trade
from ainvestor.models.schemas import (
    DecisionAction,
    PortfolioSnapshot,
    RiskCheckResult,
    TradeProposal,
    TradeSide,
)

logger = logging.getLogger(__name__)


def max_position_pct_for_conviction(conviction: int, config: dict | None = None) -> float:
    """Tamaño máximo de posición (% cartera) según convicción."""
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
    """Deterministic risk gate - AI proposals must pass all checks."""

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
    ) -> RiskCheckResult:
        reasons: list[str] = []

        if portfolio.kill_switch_active:
            reasons.append("Kill switch is active")

        if proposal.action == DecisionAction.HOLD:
            return RiskCheckResult(approved=True, proposal=proposal)

        whitelist = self.config["whitelist"]["pairs"]
        if proposal.symbol not in whitelist:
            reasons.append(f"Symbol {proposal.symbol} not in whitelist")

        if proposal.action == DecisionAction.BUY:
            reasons.extend(
                self._validate_buy(proposal, portfolio, current_price, fee_rate)
            )
        elif proposal.action == DecisionAction.SELL:
            reasons.extend(self._validate_sell(proposal, portfolio, fee_rate))

        if reasons:
            self._log_rejection(proposal, reasons, cycle_id)
            return RiskCheckResult(approved=False, proposal=proposal, rejection_reasons=reasons)

        return RiskCheckResult(approved=True, proposal=proposal)

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
            reasons.append(
                f"Insufficient balance including fee ({total_cost:.2f} USDT > "
                f"{portfolio.quote_balance:.2f} USDT)"
            )

        position_pct = (
            (order_value / portfolio.total_value_usdt) * 100
            if portfolio.total_value_usdt
            else 0
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
            reasons.append(
                f"Stop-loss {proposal.stop_loss_pct}% exceeds max {stops_cfg['max_stop_loss_pct']}%"
            )

        if proposal.take_profit_pct < stops_cfg["min_take_profit_pct"]:
            reasons.append(
                f"Take-profit {proposal.take_profit_pct}% below minimum"
            )

        min_net_gain_pct = (fee_rate * 2) * 100 + 0.5
        if proposal.take_profit_pct < min_net_gain_pct:
            reasons.append(
                f"Take-profit {proposal.take_profit_pct:.1f}% too low to cover "
                f"round-trip fees (~{fee_rate * 200:.2f}%)"
            )

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
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        count = (
            self.db.query(func.count(Trade.id))
            .filter(Trade.executed_at >= today)
            .scalar()
        ) or 0
        if count >= limits_cfg["max_trades_per_day"]:
            return [f"Max trades per day ({limits_cfg['max_trades_per_day']}) reached"]
        return []

    def _get_period_loss_pct(self, days: int) -> float:
        since = datetime.utcnow() - timedelta(days=days)
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
        """Returns list of (symbol, action, price) triggers."""
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
