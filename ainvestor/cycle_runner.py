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
from ainvestor.engine.exit_rules import mandatory_close_proposals, update_trailing_stops
from ainvestor.engine.instrument_context import build_instrument_opportunities
from ainvestor.engine.learning import DecisionLearning
from ainvestor.engine.proposal_order import proposal_execution_key, sort_proposals_for_execution
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

                fee_rate = await self.market.client.get_taker_fee_rate(proposal.symbol)

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
                )
                if check.approved:
                    success = await self.executor.execute_approved(
                        check, price, cycle_id, funding_rate=funding_rate
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

    async def run_risk_monitor(self) -> dict:
        prices: dict[str, float] = {}
        for symbol in self.market.pairs:
            try:
                ticker = await self.market.client.fetch_ticker(symbol)
                prices[symbol] = ticker.get("last") or ticker.get("close", 0)
            except Exception:
                pass

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

        portfolio = self.portfolio_mgr.get_or_create_portfolio()
        perp_sim = PerpPaperSimulator(self.db, portfolio)
        deriv_snapshots = await self.derivatives.collect()
        deriv_by_symbol = {d.symbol: d for d in deriv_snapshots}
        funding_interval = timedelta(hours=FUNDING_INTERVAL_HOURS)
        now = app_now()

        liquidated: list[str] = []
        funded: list[str] = []
        positions = self.portfolio_mgr.get_simulator().get_open_positions()
        trailing_updated = update_trailing_stops(positions, prices, self.profile)
        if trailing_updated:
            self.db.commit()

        for pos in positions:
            if getattr(pos, "instrument_type", "spot") != "perpetual":
                continue
            mark = prices.get(pos.symbol, pos.entry_price)
            if perp_sim.check_liquidation(pos, mark):
                trade = perp_sim.close_position(pos, mark, 100.0)
                if trade:
                    liquidated.append(pos.symbol)
                continue
            last_funding = pos.last_funding_at or pos.opened_at
            if last_funding and now - last_funding >= funding_interval:
                rate = deriv_by_symbol.get(pos.symbol)
                if rate:
                    perp_sim.apply_funding(pos, rate.funding_rate)
                    funded.append(pos.symbol)

        triggers = self.risk.check_stop_loss_take_profit(snapshot)
        executed = []
        for symbol, action, price in triggers:
            if action == "sell":
                success = await self.executor.execute_stop_trigger(symbol, price)
                if success:
                    executed.append(symbol)

        from ainvestor.services.charts import record_portfolio_value_async

        await record_portfolio_value_async(self.db, self.portfolio_mgr, prices)

        return {
            "profile": self.profile,
            "kill_switch": snapshot.kill_switch_active,
            "stop_triggers": executed,
            "liquidated": liquidated,
            "funding_applied": funded,
            "trailing_stops_updated": trailing_updated,
        }

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
            fee_rate = await self.market.client.get_taker_fee_rate(proposal.symbol)
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
                if await self.executor.execute_approved(check, price, cycle_id):
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
                lines.append(
                    f"  {pos.symbol} [perpetual {side} {lev}x]: margin {margin:.2f} USDT, "
                    f"notional {notional:.2f}, entry {pos.entry_price:.2f}, "
                    f"mark {pos.current_price:.2f}, PnL {pos.unrealized_pnl:+.2f}, "
                    f"ROE {roe}, liq_dist ~{liq}"
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
