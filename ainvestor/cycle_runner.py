from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from ainvestor.utils.datetime_utils import app_now, app_now_iso

from sqlalchemy.orm import Session

from ainvestor.collectors.derivatives_store import DerivativesCollector
from ainvestor.collectors.macro import MacroCollector
from ainvestor.collectors.market import MarketCollector
from ainvestor.collectors.news import NewsCollector
from ainvestor.collectors.sentiment import SentimentCollector
from ainvestor.config import get_profile_ai_cycle_interval, get_settings, load_risk_config
from ainvestor.db.models import AIDecision, CycleRun
from ainvestor.engine.ai_agent import AIAgent, build_cycle_prompt
from ainvestor.engine.executor import TradeExecutor
from ainvestor.engine.exit_rules import (
    mandatory_close_proposals,
    roe_stop_loss_triggers,
    roe_take_profit_triggers,
    update_trailing_stops,
)
from ainvestor.engine.instrument_context import build_instrument_opportunities
from ainvestor.engine.learning import DecisionLearning
from ainvestor.engine.proposal_order import is_close_proposal, proposal_execution_key, sort_proposals_for_execution
from ainvestor.engine.quant import QuantEngine
from ainvestor.engine.risk import RiskManager
from ainvestor.models.schemas import AssetClass, InstrumentType
from ainvestor.portfolio.manager import PortfolioManager
from ainvestor.portfolio.perp_simulator import FUNDING_INTERVAL_HOURS, PerpPaperSimulator
from ainvestor.portfolio.profiles import DEFAULT_PROFILE, PROFILE_EXTREME, PROFILE_LABELS, normalize_profile

logger = logging.getLogger(__name__)

_last_market_context: dict = {}


class CycleRunner:
    """Orchestrates a full AI trading cycle for one portfolio profile."""

    def __init__(self, db: Session, profile: str = DEFAULT_PROFILE):
        self.db = db
        self.profile = normalize_profile(profile)
        self.market = MarketCollector(db)
        self.news = NewsCollector(db)
        self.sentiment = SentimentCollector(db)
        self.derivatives = DerivativesCollector(db)
        self.macro = MacroCollector()
        self.quant = QuantEngine()
        self.portfolio_mgr = PortfolioManager(db, profile=self.profile)
        self.risk = RiskManager(db, profile=self.profile)
        self.executor = TradeExecutor(db, profile=self.profile)
        self.ai = AIAgent()
        self.learning = DecisionLearning(db, profile=self.profile)

    async def run(self, cycle_id: str | None = None) -> dict:
        global _last_market_context
        cycle_id = cycle_id or PortfolioManager.new_cycle_id()
        risk_config = load_risk_config(profile=self.profile)

        cycle_run = CycleRun(cycle_id=cycle_id, status="running", profile=self.profile)
        self.db.add(cycle_run)
        self.db.commit()

        try:
            tickers = await self.market.collect_all()
            prices = {t.symbol: t.last for t in tickers}

            mtf_data = await self.market.collect_all_multi_timeframe()
            signals = self.quant.analyze_all_multi(mtf_data)
            quant_map = self.quant.get_quant_conviction_map(signals)
            signals_by_symbol = self.quant.signals_by_symbol(signals)

            macro_ctx = await self.macro.collect()
            deriv_snapshots = await self.derivatives.collect_and_persist()
            deriv_by_symbol = {d.symbol: d for d in deriv_snapshots}

            news_items = await self.news.collect(currencies=self.market.pairs)
            sentiment_data = await self.sentiment.collect(btc_dominance=macro_ctx.btc_dominance)

            from ainvestor.dex import DexConnector

            dex = DexConnector()
            await dex.detect_cex_gaps(self.market.pairs)

            snapshot = await self.portfolio_mgr.get_snapshot(prices)

            await self._execute_mandatory_exits(
                snapshot, signals_by_symbol, quant_map, prices, cycle_id
            )
            snapshot = await self.portfolio_mgr.get_snapshot(prices)

            self.learning.backfill_from_decisions()
            self.learning.evaluate_pending(prices)
            learning_summary = self.learning.build_learning_summary()

            instrument_context = build_instrument_opportunities(
                prices,
                deriv_snapshots,
                signals,
                quant_map,
                snapshot,
                self.profile,
                db=self.db,
            )

            use_mcp = self.ai.settings.ai_use_mcp and bool(self.ai.settings.cursor_api_key)
            prompt = build_cycle_prompt(
                portfolio_summary=self._format_portfolio(snapshot),
                market_summary=self._format_market(tickers),
                signals_summary=self.quant.summarize(signals),
                news_summary=self.news.summarize(news_items),
                sentiment_summary=self.sentiment.summarize(
                    sentiment_data, macro_ctx.btc_dominance
                ),
                risk_config=risk_config,
                learning_summary=learning_summary,
                macro_summary=self.macro.summarize(macro_ctx),
                derivatives_summary=self.derivatives.summarize(deriv_snapshots, prices),
                instrument_context=instrument_context,
                quant_reference=self._format_quant_reference(signals, quant_map, risk_config),
                market_status="crypto-only",
                use_mcp=use_mcp,
                profile=self.profile,
                ai_cycle_interval_minutes=get_profile_ai_cycle_interval(self.profile),
                risk_monitor_interval_minutes=get_settings().risk_monitor_interval,
                price_tick_interval_seconds=get_settings().price_tick_interval_seconds,
            )

            if self.profile == PROFILE_EXTREME:
                ctx = {
                    "tickers": [t.model_dump(mode="json") for t in tickers],
                    "signals": [s.model_dump() for s in signals],
                    "derivatives": [d.model_dump(mode="json") for d in deriv_snapshots],
                    "macro": macro_ctx.model_dump(mode="json"),
                    "sentiment": sentiment_data.model_dump(mode="json"),
                    "news": [n.model_dump(mode="json") for n in news_items[:10]],
                    "profile": self.profile,
                    "market_status": "crypto-only",
                    "captured_at": app_now_iso(),
                }
                _last_market_context = ctx
                from ainvestor.services.market_context_cache import persist_market_context

                persist_market_context(self.db, ctx)

            decision, raw_response, run_id, token_usage = await self.ai.run_cycle(prompt)

            approved_count = 0
            rejected_count = 0
            approved_keys: set[tuple[str, str, str, str]] = set()
            rejected_proposals: list[tuple] = []

            ordered_proposals = sort_proposals_for_execution(decision.proposals, snapshot)

            for proposal in ordered_proposals:
                if (
                    proposal.instrument_type == InstrumentType.STOCK
                    or proposal.asset_class == AssetClass.STOCK
                ):
                    rejected_count += 1
                    rejected_proposals.append((proposal, ["Stock trades disabled"]))
                    continue

                price = prices.get(proposal.symbol, 0)
                if price <= 0:
                    rejected_count += 1
                    rejected_proposals.append((proposal, ["Precio no disponible"]))
                    continue

                fee_rate = await self.market.client.get_taker_fee_rate(
                    proposal.symbol, proposal.instrument_type.value
                )

                funding_rate = 0.0
                deriv = deriv_by_symbol.get(proposal.symbol)
                derivatives_available = deriv is not None
                if deriv:
                    funding_rate = deriv.funding_rate

                check = self.risk.validate_proposal(
                    proposal,
                    snapshot,
                    price,
                    cycle_id,
                    fee_rate=fee_rate,
                    quant_conviction=quant_map.get(proposal.symbol),
                    quant_map=quant_map,
                    funding_rate=funding_rate,
                    derivatives_available=derivatives_available,
                    cycle_proposals=ordered_proposals,
                    signal=signals_by_symbol.get(proposal.symbol),
                )
                if check.approved:
                    close_reason = None
                    if is_close_proposal(proposal, snapshot):
                        close_reason = "ai_discretionary"
                    success = await self.executor.execute_approved(
                        check, price, cycle_id, funding_rate=funding_rate, close_reason=close_reason
                    )
                    if success:
                        approved_count += 1
                        approved_keys.add(proposal_execution_key(proposal))
                        snapshot = await self.portfolio_mgr.get_snapshot(prices)
                    else:
                        rejected_count += 1
                        rejected_proposals.append((proposal, ["Ejecución fallida"]))
                else:
                    rejected_count += 1
                    rejected_proposals.append((proposal, check.rejection_reasons))

            self.learning.record_cycle(
                cycle_id=cycle_id,
                decision=decision,
                prices=prices,
                approved_keys=approved_keys,
                rejected=rejected_proposals,
                open_positions=snapshot.positions,
            )

            ai_record = AIDecision(
                cycle_id=cycle_id,
                profile=self.profile,
                model=self.ai.settings.effective_ai_model(),
                summary=decision.summary,
                hold=decision.hold,
                prompt_summary=prompt[:2000],
                raw_response=raw_response[:10000] if raw_response else None,
                proposals_json=json.dumps([p.model_dump() for p in decision.proposals]),
                approved_count=approved_count,
                rejected_count=rejected_count,
                run_id=run_id,
                tokens_input=token_usage.input_tokens,
                tokens_output=token_usage.output_tokens,
                tokens_cache_read=token_usage.cache_read_tokens,
                tokens_cache_write=token_usage.cache_write_tokens,
                tokens_total=token_usage.total_tokens,
            )
            self.db.add(ai_record)

            from ainvestor.services.charts import record_portfolio_value_async

            await record_portfolio_value_async(self.db, self.portfolio_mgr, prices)

            cycle_run.status = "completed"
            cycle_run.completed_at = app_now()
            self.db.commit()

            return {
                "cycle_id": cycle_id,
                "profile": self.profile,
                "profile_label": PROFILE_LABELS.get(self.profile, self.profile),
                "status": "completed",
                "hold": decision.hold,
                "summary": decision.summary,
                "allocation": decision.allocation,
                "proposals": len(decision.proposals),
                "approved": approved_count,
                "rejected": rejected_count,
                "run_id": run_id,
                "token_usage": token_usage.to_dict(),
                "total_value_usdt": snapshot.total_value_usdt,
            }

        except Exception as e:
            logger.exception("Cycle %s (%s) failed: %s", cycle_id, self.profile, e)
            cycle_run.status = "error"
            cycle_run.error = str(e)
            cycle_run.completed_at = app_now()
            self.db.commit()
            return {
                "cycle_id": cycle_id,
                "profile": self.profile,
                "status": "error",
                "error": str(e),
            }

    def _collect_exit_triggers(self, snapshot) -> list[tuple[str, str, float, str]]:
        """Price/ROE exit triggers with close_reason for audit."""
        triggers: list[tuple[str, str, float, str]] = []
        seen: set[str] = set()

        for pos in snapshot.positions:
            side = getattr(pos, "position_side", "long") or "long"
            if side == "short":
                if pos.stop_loss and pos.current_price >= pos.stop_loss:
                    triggers.append((pos.symbol, "sell", pos.current_price, "risk_sl"))
                    seen.add(pos.symbol)
                elif pos.take_profit and pos.current_price <= pos.take_profit:
                    triggers.append((pos.symbol, "sell", pos.current_price, "risk_tp"))
                    seen.add(pos.symbol)
            else:
                if pos.stop_loss and pos.current_price <= pos.stop_loss:
                    triggers.append((pos.symbol, "sell", pos.current_price, "risk_sl"))
                    seen.add(pos.symbol)
                elif pos.take_profit and pos.current_price >= pos.take_profit:
                    triggers.append((pos.symbol, "sell", pos.current_price, "risk_tp"))
                    seen.add(pos.symbol)

        for symbol, price in roe_take_profit_triggers(snapshot, self.profile):
            if symbol not in seen:
                triggers.append((symbol, "sell", price, "risk_roe_tp"))
                seen.add(symbol)
        for symbol, price in roe_stop_loss_triggers(snapshot, self.profile):
            if symbol not in seen:
                triggers.append((symbol, "sell", price, "risk_roe_sl"))
                seen.add(symbol)
        return triggers

    async def run_price_risk_check(self) -> dict:
        """Fast SL/TP/liquidation using in-memory price cache (every ~5s)."""
        from ainvestor.services.market_prices import get_open_position_symbols
        from ainvestor.services.price_cache import get_prices

        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        open_syms = get_open_position_symbols(self.db, portfolio.id)
        if not open_syms:
            return {"profile": self.profile, "skip": "no_positions"}

        prices = get_prices(list(open_syms))
        if not prices:
            return {"profile": self.profile, "skip": "no_prices"}

        snapshot = await self.portfolio_mgr.get_snapshot(prices)
        perp_sim = PerpPaperSimulator(self.db, portfolio)

        liquidated: list[str] = []
        positions = self.portfolio_mgr.get_simulator().get_open_positions()
        trailing_updated = update_trailing_stops(positions, prices, self.profile)
        if trailing_updated:
            self.db.commit()

        for pos in positions:
            if getattr(pos, "instrument_type", "spot") != "perpetual":
                continue
            mark = prices.get(pos.symbol, pos.entry_price)
            if perp_sim.check_liquidation(pos, mark):
                trade = perp_sim.close_position(pos, mark, 100.0, close_reason="liquidation")
                if trade:
                    liquidated.append(pos.symbol)

        snapshot = await self.portfolio_mgr.get_snapshot(prices)
        triggers = self._collect_exit_triggers(snapshot)

        executed: list[dict] = []
        for symbol, action, price, reason in triggers:
            if action == "sell":
                success = await self.executor.execute_stop_trigger(
                    symbol, price, close_reason=reason
                )
                if success:
                    executed.append({"symbol": symbol, "reason": reason})

        if executed or liquidated:
            from ainvestor.services.charts import record_portfolio_value_async

            await record_portfolio_value_async(self.db, self.portfolio_mgr, prices)

        return {
            "profile": self.profile,
            "stop_triggers": executed,
            "liquidated": liquidated,
            "trailing_stops_updated": trailing_updated,
        }

    async def run_drawdown_check(self) -> dict:
        """Kill switch on max drawdown (no exchange calls)."""
        from ainvestor.services.market_prices import get_open_position_symbols
        from ainvestor.services.price_cache import get_prices

        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        open_syms = get_open_position_symbols(self.db, portfolio.id)
        prices = get_prices(list(open_syms)) if open_syms else {}
        snapshot = await self.portfolio_mgr.get_snapshot(prices)

        if self.risk.should_activate_kill_switch(snapshot):
            self.portfolio_mgr.set_kill_switch(True)
            logger.warning("Kill switch activated (%s) due to max drawdown", self.profile)
            from ainvestor.alerts import send_telegram_alert

            label = PROFILE_LABELS.get(self.profile, self.profile)
            await send_telegram_alert(
                f"AInvestor ({label}): Kill switch activated (max drawdown)"
            )
            return {"profile": self.profile, "kill_switch": True, "reason": "max_drawdown"}

        return {"profile": self.profile, "kill_switch": snapshot.kill_switch_active}

    async def run_funding_check(self) -> dict:
        """Apply perp funding payments (infrequent, uses derivatives API)."""
        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        perp_sim = PerpPaperSimulator(self.db, portfolio)
        deriv_snapshots = await self.derivatives.collect()
        deriv_by_symbol = {d.symbol: d for d in deriv_snapshots}
        funding_interval = timedelta(hours=FUNDING_INTERVAL_HOURS)
        now = app_now()

        funded: list[str] = []
        positions = self.portfolio_mgr.get_simulator().get_open_positions()
        for pos in positions:
            if getattr(pos, "instrument_type", "spot") != "perpetual":
                continue
            last_funding = pos.last_funding_at or pos.opened_at
            if last_funding and now - last_funding >= funding_interval:
                rate = deriv_by_symbol.get(pos.symbol)
                if rate:
                    perp_sim.apply_funding(pos, rate.funding_rate)
                    funded.append(pos.symbol)

        return {"profile": self.profile, "funding_applied": funded}

    async def _execute_mandatory_exits(
        self,
        snapshot,
        signals_by_symbol: dict,
        quant_map: dict[str, int],
        prices: dict[str, float],
        cycle_id: str,
    ) -> int:
        """Ejecuta cierres obligatorios por ROE antes de llamar a la IA."""
        mandatory = mandatory_close_proposals(
            snapshot, signals_by_symbol, quant_map, self.profile
        )
        if not mandatory:
            return 0

        executed = 0
        for proposal in mandatory:
            price = prices.get(proposal.symbol, 0)
            if price <= 0:
                continue
            fee_rate = await self.market.client.get_taker_fee_rate(
                proposal.symbol, proposal.instrument_type.value
            )
            check = self.risk.validate_proposal(
                proposal,
                snapshot,
                price,
                cycle_id,
                fee_rate=fee_rate,
                quant_conviction=quant_map.get(proposal.symbol),
                quant_map=quant_map,
                derivatives_available=True,
                cycle_proposals=mandatory,
            )
            if check.approved:
                if await self.executor.execute_approved(
                    check, price, cycle_id, close_reason="ai_mandatory"
                ):
                    executed += 1
                    snapshot = await self.portfolio_mgr.get_snapshot(prices)
        if executed:
            logger.info("Mandatory exits executed: %d (%s)", executed, self.profile)
        return executed

    def _format_quant_reference(
        self, signals, quant_map: dict[str, int], risk_config: dict
    ) -> str:
        ai_cfg = risk_config.get("ai_validation", {})
        threshold = int(ai_cfg.get("conviction_divergence_threshold", 30))
        min_conv = int(ai_cfg.get("min_conviction_on_divergence", 80))
        lines = [
            f"Divergence threshold: {threshold} pts | min conviction if diverging: {min_conv}",
        ]
        for s in signals:
            qc = quant_map.get(s.symbol, s.conviction_score)
            div = ""
            lines.append(
                f"{s.symbol}: quant={qc} trend={s.trend} conviction_score={s.conviction_score}{div}"
            )
        return "\n".join(lines) if lines else "No quant reference."

    def _format_portfolio(self, snapshot) -> str:
        label = PROFILE_LABELS.get(self.profile, self.profile)
        lines = [
            f"Profile: {label} ({snapshot.profile})",
            f"Mode: {snapshot.mode.value}",
            f"Quote balance (available margin): {snapshot.quote_balance:.2f} USDT",
            f"Total value: {snapshot.total_value_usdt:.2f} USDT",
            f"Invested (spot value + perp margin): {snapshot.invested_usdt:.2f} USDT",
            f"Unrealized P&L: {snapshot.unrealized_pnl:.2f}",
            f"Realized P&L: {snapshot.realized_pnl:.2f}",
            f"Kill switch: {snapshot.kill_switch_active}",
        ]
        for pos in snapshot.positions:
            inst = getattr(pos, "instrument_type", "spot") or "spot"
            side = getattr(pos, "position_side", "long") or "long"
            lev = getattr(pos, "leverage", 1) or 1
            if inst == "perpetual":
                margin = pos.margin_used or 0
                notional = pos.notional_usdt or 0
                roe = f"{pos.roe_pct:+.1f}%" if pos.roe_pct is not None else "N/A"
                liq = f"{pos.liq_distance_pct:.0f}%" if pos.liq_distance_pct is not None else "N/A"
                exit_cfg = load_risk_config(profile=self.profile).get("exit_rules", {})
                tp_roe = float(exit_cfg.get("take_profit_roe_pct", 6.0))
                sl_roe = float(exit_cfg.get("stop_loss_roe_pct", -6.0))
                tick = get_settings().price_tick_interval_seconds
                lines.append(
                    f"  {pos.symbol} [perpetual {side} {lev}x]: margin {margin:.2f} USDT, "
                    f"notional {notional:.2f}, entry {pos.entry_price:.2f}, "
                    f"mark {pos.current_price:.2f}, PnL {pos.unrealized_pnl:+.2f}, "
                    f"ROE {roe}, liq_dist ~{liq}, "
                    f"auto-TP +{tp_roe:.0f}% ROE / auto-SL {sl_roe:.0f}% ROE (monitor every {tick}s when open)"
                )
            else:
                lines.append(
                    f"  {pos.symbol} [spot long]: {pos.amount:.6f} @ {pos.entry_price:.2f} "
                    f"(now {pos.current_price:.2f}, PnL {pos.unrealized_pnl:+.2f})"
                )
        return "\n".join(lines)

    def _format_market(self, tickers) -> str:
        lines = ["--- Crypto ---"]
        sorted_tickers = sorted(tickers, key=lambda t: abs(t.change_pct or 0), reverse=True)
        for t in sorted_tickers[:12]:
            chg = f"{t.change_pct:+.2f}%" if t.change_pct else "N/A"
            spread = f", spread {t.spread_pct:.3f}%" if t.spread_pct else ""
            lines.append(f"{t.symbol}: {t.last:.4f} ({chg}{spread})")
        return "\n".join(lines)


def get_last_market_context() -> dict:
    return _last_market_context
