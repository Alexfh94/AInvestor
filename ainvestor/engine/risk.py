from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy import func
from sqlalchemy.orm import Session

from ainvestor.config import get_profile_ai_cycle_interval, get_settings, load_risk_config
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
from ainvestor.engine.exit_rules import is_rotation_open
from ainvestor.engine.proposal_order import is_close_proposal
from ainvestor.portfolio.perp_sizing import compute_all_in_perp_open
from ainvestor.portfolio.profiles import DEFAULT_PROFILE, PROFILE_EXTREME, normalize_profile
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

    def __init__(self, db: Session, profile: str = DEFAULT_PROFILE):
        self.db = db
        self.profile = normalize_profile(profile)
        self.config = load_risk_config(profile=self.profile)
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
        quant_map: dict[str, int] | None = None,
        funding_rate: float = 0.0,
        derivatives_available: bool = True,
        cycle_proposals: list[TradeProposal] | None = None,
    ) -> RiskCheckResult:
        reasons: list[str] = []

        if portfolio.kill_switch_active:
            reasons.append("Kill switch is active")

        if self._global_kill_switch_active():
            reasons.append("Global kill switch active (risk.yaml)")

        if proposal.action == DecisionAction.HOLD:
            return RiskCheckResult(approved=True, proposal=proposal)

        if (
            self.profile == PROFILE_EXTREME
            and proposal.instrument_type != InstrumentType.PERPETUAL
        ):
            reasons.append("Extreme profile: only perpetuals allowed (no spot)")
            self._log_rejection(proposal, reasons, cycle_id, portfolio.portfolio_id)
            return RiskCheckResult(approved=False, proposal=proposal, rejection_reasons=reasons)

        if proposal.instrument_type == InstrumentType.STOCK or proposal.asset_class == AssetClass.STOCK:
            reasons.append("Stock trades disabled for dual-portfolio comparison")
            self._log_rejection(proposal, reasons, cycle_id, portfolio.portfolio_id)
            return RiskCheckResult(approved=False, proposal=proposal, rejection_reasons=reasons)

        reasons.extend(self._check_live_mode_allowed(proposal))
        reasons.extend(self._check_conviction_divergence(proposal, quant_conviction))
        reasons.extend(self._check_min_conviction(proposal, quant_conviction))

        if proposal.instrument_type == InstrumentType.PERPETUAL:
            reasons.extend(
                self._validate_perp(
                    proposal,
                    portfolio,
                    current_price,
                    fee_rate,
                    funding_rate,
                    cycle_proposals or [],
                    derivatives_available,
                    quant_conviction=quant_conviction,
                    quant_map=quant_map,
                )
            )
        else:
            reasons.extend(self._validate_spot(proposal, portfolio, current_price, fee_rate, cycle_id))

        if reasons:
            self._log_rejection(proposal, reasons, cycle_id, portfolio.portfolio_id)
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
        funding_rate: float,
        cycle_proposals: list[TradeProposal],
        derivatives_available: bool = True,
        quant_conviction: int | None = None,
        quant_map: dict[str, int] | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        deriv = self.config.get("derivatives", {})
        if not deriv.get("enabled", False):
            reasons.append("Derivatives disabled in risk config")
        if deriv.get("paper_only", True) and self.settings.trading_mode == "live":
            live_cfg = self.config.get("modes", {}).get("live", {})
            if not live_cfg.get("gradual_activation", {}).get("crypto_perps", False):
                reasons.append("Live perps not activated (gradual_activation.crypto_perps=false)")

        whitelist = self.config["whitelist"]["pairs"]
        if proposal.symbol not in whitelist:
            reasons.append(f"Symbol {proposal.symbol} not in whitelist")

        max_lev = int(deriv.get("max_leverage", 2))
        mifid_cap = int(deriv.get("mifid_retail_leverage_cap", 2))
        cap = min(max_lev, mifid_cap)
        if proposal.leverage > cap:
            reasons.append(f"Leverage {proposal.leverage}x exceeds cap {cap}x")

        portfolio_id = portfolio.portfolio_id
        open_perps = (
            self.db.query(func.count(Position.id))
            .filter(
                Position.portfolio_id == portfolio_id,
                Position.is_open == True,  # noqa: E712
                Position.instrument_type == "perpetual",
            )
            .scalar()
        ) or 0

        is_opening = (
            (proposal.action == DecisionAction.BUY and proposal.position_side == "long")
            or (proposal.action == DecisionAction.SELL and proposal.position_side == "short")
        )
        existing_perp = next(
            (
                p
                for p in portfolio.positions
                if p.symbol == proposal.symbol and p.instrument_type == "perpetual"
            ),
            None,
        )
        if is_opening and existing_perp is None:
            if open_perps >= int(deriv.get("max_open_perps", 2)):
                reasons.append("Max open perpetual positions reached")

        if is_opening:
            if not derivatives_available:
                reasons.append(
                    "Derivatives data (funding/OI) unavailable — perpetual open blocked"
                )
            reasons.extend(self._check_extreme_all_in(proposal, is_close=False))
            reasons.extend(
                self._validate_buy(
                    proposal,
                    portfolio,
                    current_price,
                    fee_rate,
                    is_perp=True,
                )
            )
            reasons.extend(self._check_perp_funding(proposal, funding_rate, deriv))
            reasons.extend(
                self._check_spot_perp_conflict(proposal, portfolio, cycle_proposals)
            )
            reasons.extend(
                self._check_reentry_cooldown(proposal, portfolio.portfolio_id)
            )
            reasons.extend(
                self._check_rotation_edge(
                    proposal, portfolio, cycle_proposals, quant_conviction, quant_map
                )
            )
        else:
            reasons.extend(self._validate_perp_close(proposal, portfolio, fee_rate))
            reasons.extend(self._check_extreme_all_in(proposal, is_close=True))
        return reasons

    def _check_extreme_all_in(
        self, proposal: TradeProposal, *, is_close: bool = False
    ) -> list[str]:
        if self.profile != PROFILE_EXTREME:
            return []
        required = float(
            self.config.get("profit_optimization", {}).get("extreme_all_in_amount_pct", 100.0)
        )
        if abs(proposal.amount_pct - required) > 0.01:
            action = "close" if is_close else "open"
            return [
                f"Extreme all-in: {action} requires amount_pct={required:.0f} "
                f"(got {proposal.amount_pct:.1f})"
            ]
        return []

    def _check_stop_loss_vs_leverage(self, proposal: TradeProposal) -> list[str]:
        if proposal.leverage <= 0:
            return []
        min_sl = 100.0 / proposal.leverage
        if proposal.stop_loss_pct < min_sl - 0.01:
            return [
                f"Stop-loss {proposal.stop_loss_pct:.2f}% below minimum {min_sl:.2f}% "
                f"for {proposal.leverage}x leverage (max margin loss)"
            ]
        return []

    def _check_perp_funding(
        self, proposal: TradeProposal, funding_rate: float, deriv: dict
    ) -> list[str]:
        reasons: list[str] = []
        warning_pct = float(deriv.get("funding_cost_warning_pct", 0.05)) / 100
        fr = abs(funding_rate)
        if proposal.position_side == "long" and funding_rate > warning_pct:
            reasons.append(
                f"Funding rate {funding_rate * 100:.4f}% too high for long perp "
                f"(warning {deriv.get('funding_cost_warning_pct', 0.05)}%)"
            )
        if proposal.position_side == "short" and funding_rate < -warning_pct:
            reasons.append(
                f"Funding rate {funding_rate * 100:.4f}% negative — shorts pay, unfavorable"
            )
        return reasons

    def _check_spot_perp_conflict(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        cycle_proposals: list[TradeProposal],
    ) -> list[str]:
        if proposal.position_side != "long":
            return []
        spot_pos = next(
            (
                p
                for p in portfolio.positions
                if p.symbol == proposal.symbol and p.instrument_type == "spot"
            ),
            None,
        )
        if spot_pos is None:
            return []
        closing_spot = any(
            p.symbol == proposal.symbol
            and p.action == DecisionAction.SELL
            and p.instrument_type == InstrumentType.SPOT
            for p in cycle_proposals
        )
        if closing_spot:
            return []
        return [
            f"Spot long open on {proposal.symbol} — close spot first or use perp short as hedge"
        ]

    def _validate_perp_close(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        fee_rate: float,
    ) -> list[str]:
        reasons: list[str] = []
        position = next(
            (
                p
                for p in portfolio.positions
                if p.symbol == proposal.symbol and p.instrument_type == "perpetual"
            ),
            None,
        )
        if position is None:
            reasons.append(f"No open perp position for {proposal.symbol}")
            return reasons
        if position.position_side == "long" and proposal.action != DecisionAction.SELL:
            reasons.append("Close perp long with SELL")
        if position.position_side == "short" and proposal.action != DecisionAction.BUY:
            reasons.append("Close perp short with BUY")
        close_notional = (position.notional_usdt or position.value_usdt) * (
            proposal.amount_pct / 100
        )
        fee = close_notional * fee_rate
        if close_notional - fee <= 0:
            reasons.append("Close value too small after fees")
        return reasons

    def _validate_stock(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        current_price: float,
    ) -> list[str]:
        return ["Stock trades disabled for dual-portfolio comparison"]

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

    def _check_min_conviction(
        self, proposal: TradeProposal, quant_conviction: int | None
    ) -> list[str]:
        if proposal.action == DecisionAction.HOLD:
            return []
        is_opening = (
            (proposal.action == DecisionAction.BUY and proposal.position_side == "long")
            or (proposal.action == DecisionAction.SELL and proposal.position_side == "short")
        )
        if not is_opening:
            return []

        ai_cfg = self.config.get("ai_validation", {})
        min_conv = int(ai_cfg.get("min_conviction", 60))
        min_quant = int(ai_cfg.get("min_quant_conviction_entry", 55))
        reasons: list[str] = []
        if proposal.conviction < min_conv:
            reasons.append(
                f"Conviction {proposal.conviction} below minimum {min_conv} to open"
            )
        if quant_conviction is not None and quant_conviction < min_quant:
            reasons.append(
                f"Quant conviction {quant_conviction} below {min_quant} — stay in cash"
            )
        return reasons

    def _check_reentry_cooldown(
        self, proposal: TradeProposal, portfolio_id: int
    ) -> list[str]:
        exit_cfg = self.config.get("exit_rules", {})
        cooldown_cycles = int(exit_cfg.get("reentry_cooldown_cycles", 2))
        interval = get_profile_ai_cycle_interval(self.profile)
        since = app_now() - timedelta(minutes=interval * cooldown_cycles)

        last_close = (
            self.db.query(Trade)
            .filter(
                Trade.portfolio_id == portfolio_id,
                Trade.symbol == proposal.symbol,
                Trade.trade_action == "close",
                Trade.executed_at >= since,
            )
            .order_by(Trade.executed_at.desc())
            .first()
        )
        if last_close is None:
            return []
        if last_close.position_side != proposal.position_side:
            return []
        return [
            f"Re-entry cooldown: {proposal.symbol} {proposal.position_side} closed within "
            f"{cooldown_cycles} cycles ({interval * cooldown_cycles} min)"
        ]

    def _check_rotation_edge(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        cycle_proposals: list[TradeProposal],
        quant_conviction: int | None,
        quant_map: dict[str, int] | None,
    ) -> list[str]:
        if not is_rotation_open(proposal, cycle_proposals, portfolio):
            return []

        exit_cfg = self.config.get("exit_rules", {})
        min_delta = int(exit_cfg.get("rotation_min_conviction_delta", 15))
        qmap = quant_map or {}
        new_conv = qmap.get(proposal.symbol, quant_conviction or proposal.conviction)

        for close_p in cycle_proposals:
            if not is_close_proposal(close_p, portfolio):
                continue
            if close_p.symbol == proposal.symbol:
                continue

            closed_pos = next(
                (p for p in portfolio.positions if p.symbol == close_p.symbol),
                None,
            )
            if closed_pos and closed_pos.roe_pct is not None and closed_pos.roe_pct >= 8.0:
                return [
                    f"Rotation blocked: {close_p.symbol} still profitable "
                    f"(+{closed_pos.roe_pct:.1f}% ROE) — hold or wait for exit rule"
                ]

            old_conv = qmap.get(close_p.symbol, 50)
            delta = new_conv - old_conv
            if delta < min_delta:
                return [
                    f"Rotation blocked: conviction delta {delta} < {min_delta} "
                    f"({close_p.symbol} quant {old_conv} → {proposal.symbol} quant {new_conv})"
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
        is_perp: bool = False,
    ) -> list[str]:
        reasons: list[str] = []
        pos_cfg = self.config["position"]
        stops_cfg = self.config["stops"]
        limits_cfg = self.config["limits"]

        open_positions = len(portfolio.positions)
        if open_positions >= pos_cfg["max_open_positions"]:
            reasons.append(f"Max open positions ({pos_cfg['max_open_positions']}) reached")

        margin_or_quote = portfolio.quote_balance * (proposal.amount_pct / 100)
        if is_perp:
            if self.profile == PROFILE_EXTREME and proposal.amount_pct >= 99.99:
                margin_or_quote = self._extreme_all_in_margin(
                    portfolio.quote_balance, proposal.leverage, fee_rate
                )
            notional = margin_or_quote * proposal.leverage
            order_value = margin_or_quote
            total_cost = order_value + notional * fee_rate
            min_order = pos_cfg["min_order_value_usdt"]
            if margin_or_quote < min_order:
                reasons.append(f"Margin {margin_or_quote:.2f} below minimum")
        else:
            order_value = margin_or_quote
            total_cost = order_value * (1 + fee_rate)

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

        if not is_perp and order_value < pos_cfg["min_order_value_usdt"]:
            reasons.append(f"Order value {order_value:.2f} below minimum")

        if stops_cfg["require_stop_loss"] and proposal.stop_loss_pct <= 0:
            reasons.append("Stop-loss is required")
        if stops_cfg["require_take_profit"] and proposal.take_profit_pct <= 0:
            reasons.append("Take-profit is required")

        if proposal.stop_loss_pct > stops_cfg["max_stop_loss_pct"]:
            reasons.append(f"Stop-loss exceeds max {stops_cfg['max_stop_loss_pct']}%")

        min_tp = float(stops_cfg.get("min_take_profit_pct", 1.0))
        if is_perp:
            min_tp = min(min_tp, float(stops_cfg.get("min_take_profit_pct_perp", 0.35)))
        if proposal.take_profit_pct < min_tp:
            reasons.append(f"Take-profit below minimum ({min_tp}%)")

        if is_perp:
            max_tp = float(stops_cfg.get("max_take_profit_pct_perp", 1.5))
            if proposal.take_profit_pct > max_tp:
                reasons.append(
                    f"Take-profit {proposal.take_profit_pct:.2f}% exceeds max {max_tp}% for perps"
                )

        opt = self.config.get("profit_optimization", {})
        fee_multiplier = float(opt.get("min_tp_fee_multiplier", 2.0))
        round_trip_pct = (fee_rate * 2) * 100
        margin_pct = 0.25 if is_perp else 0.5
        min_net_gain_pct = round_trip_pct * fee_multiplier + margin_pct
        if proposal.take_profit_pct < min_net_gain_pct:
            reasons.append(
                f"Take-profit {proposal.take_profit_pct:.2f}% below minimum net edge "
                f"({min_net_gain_pct:.2f}% after round-trip fees)"
            )

        reasons.extend(self._check_loss_limits(portfolio, limits_cfg))
        reasons.extend(self._check_trade_count_limits(limits_cfg, portfolio.portfolio_id))
        if is_perp:
            reasons.extend(self._check_stop_loss_vs_leverage(proposal))
        return reasons

    def _extreme_all_in_margin(
        self, quote_balance: float, leverage: int, fee_rate: float
    ) -> float:
        """Margin that uses balance minus tiny fee reserve after opening fee on notional."""
        reserve = float(self.config.get("fees", {}).get("all_in_reserve_pct", 0.1))
        margin, _, _ = compute_all_in_perp_open(
            quote_balance, leverage, fee_rate, fee_reserve_pct=reserve
        )
        return margin

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
        reasons.extend(self._check_extreme_all_in(proposal, is_close=True))
        return reasons

    def _check_loss_limits(self, portfolio: PortfolioSnapshot, limits_cfg: dict) -> list[str]:
        reasons: list[str] = []
        initial = self._initial_for_portfolio(portfolio)
        if initial <= 0:
            return reasons

        drawdown_pct = ((initial - portfolio.total_value_usdt) / initial) * 100
        if drawdown_pct >= limits_cfg["max_drawdown_pct"]:
            reasons.append(f"Max drawdown {drawdown_pct:.1f}% exceeded")

        daily_loss = self._get_period_loss_pct(days=1, portfolio_id=portfolio.portfolio_id, initial=initial)
        if daily_loss >= limits_cfg["max_daily_loss_pct"]:
            reasons.append(f"Daily loss limit {daily_loss:.1f}% exceeded")

        weekly_loss = self._get_period_loss_pct(days=7, portfolio_id=portfolio.portfolio_id, initial=initial)
        if weekly_loss >= limits_cfg["max_weekly_loss_pct"]:
            reasons.append(f"Weekly loss limit {weekly_loss:.1f}% exceeded")

        return reasons

    def _get_initial_from_snapshot(self, portfolio: PortfolioSnapshot) -> float:
        from ainvestor.portfolio.manager import PortfolioManager

        return PortfolioManager(self.db, profile=self.profile).get_initial_value()

    def _initial_for_portfolio(self, portfolio: PortfolioSnapshot) -> float:
        from ainvestor.db.models import Portfolio as PortfolioModel

        row = self.db.query(PortfolioModel).filter(PortfolioModel.id == portfolio.portfolio_id).first()
        if row and row.initial_balance:
            return row.initial_balance
        return self._get_initial_from_snapshot(portfolio)

    def _check_trade_count_limits(self, limits_cfg: dict, portfolio_id: int) -> list[str]:
        today = app_now().replace(hour=0, minute=0, second=0, microsecond=0)
        count = (
            self.db.query(func.count(Trade.id))
            .filter(Trade.executed_at >= today, Trade.portfolio_id == portfolio_id)
            .scalar()
        ) or 0
        if count >= limits_cfg["max_trades_per_day"]:
            return [f"Max trades per day ({limits_cfg['max_trades_per_day']}) reached"]
        return []

    def _get_period_loss_pct(self, days: int, portfolio_id: int, initial: float) -> float:
        since = app_now() - timedelta(days=days)
        sells = (
            self.db.query(Trade)
            .filter(
                Trade.executed_at >= since,
                Trade.side == TradeSide.SELL.value,
                Trade.portfolio_id == portfolio_id,
            )
            .all()
        )
        if not sells:
            return 0.0
        total_pnl = sum(t.value_usdt - t.fee for t in sells)
        if total_pnl >= 0:
            return 0.0
        return abs(total_pnl / initial) * 100

    def check_stop_loss_take_profit(
        self, portfolio: PortfolioSnapshot
    ) -> list[tuple[str, str, float]]:
        triggers: list[tuple[str, str, float]] = []
        for pos in portfolio.positions:
            side = getattr(pos, "position_side", "long") or "long"
            if side == "short":
                if pos.stop_loss and pos.current_price >= pos.stop_loss:
                    triggers.append((pos.symbol, "sell", pos.current_price))
                elif pos.take_profit and pos.current_price <= pos.take_profit:
                    triggers.append((pos.symbol, "sell", pos.current_price))
            else:
                if pos.stop_loss and pos.current_price <= pos.stop_loss:
                    triggers.append((pos.symbol, "sell", pos.current_price))
                elif pos.take_profit and pos.current_price >= pos.take_profit:
                    triggers.append((pos.symbol, "sell", pos.current_price))
        return triggers

    def should_activate_kill_switch(self, portfolio: PortfolioSnapshot) -> bool:
        initial = self._initial_for_portfolio(portfolio)
        if initial <= 0:
            return False
        drawdown = ((initial - portfolio.total_value_usdt) / initial) * 100
        return drawdown >= self.config["limits"]["max_drawdown_pct"]

    def _log_rejection(
        self,
        proposal: TradeProposal,
        reasons: list[str],
        cycle_id: str | None,
        portfolio_id: int | None = None,
    ) -> None:
        event = RiskEvent(
            event_type="proposal_rejected",
            symbol=proposal.symbol,
            details="; ".join(reasons),
            cycle_id=cycle_id,
            portfolio_id=portfolio_id,
        )
        self.db.add(event)
        self.db.commit()
        logger.info("Rejected %s %s: %s", proposal.action, proposal.symbol, reasons)
