from __future__ import annotations

import json
import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now, app_now_iso

from sqlalchemy.orm import Session

from ainvestor.collectors.derivatives_store import DerivativesCollector
from ainvestor.collectors.macro import MacroCollector
from ainvestor.collectors.market import MarketCollector
from ainvestor.collectors.news import NewsCollector
from ainvestor.collectors.sentiment import SentimentCollector
from ainvestor.config import load_risk_config
from ainvestor.db.models import AIDecision, CycleRun
from ainvestor.engine.ai_agent import AIAgent, build_cycle_prompt
from ainvestor.engine.executor import TradeExecutor
from ainvestor.engine.learning import DecisionLearning
from ainvestor.engine.quant import QuantEngine
from ainvestor.engine.risk import RiskManager
from ainvestor.models.schemas import AssetClass, InstrumentType
from ainvestor.portfolio.manager import PortfolioManager
from ainvestor.portfolio.profiles import DEFAULT_PROFILE, PROFILE_LABELS, normalize_profile

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

            macro_ctx = await self.macro.collect()
            deriv_snapshots = await self.derivatives.collect_and_persist()
            deriv_by_symbol = {d.symbol: d for d in deriv_snapshots}

            news_items = await self.news.collect(currencies=self.market.pairs)
            sentiment_data = await self.sentiment.collect(btc_dominance=macro_ctx.btc_dominance)

            from ainvestor.dex import DexConnector

            dex = DexConnector()
            await dex.detect_cex_gaps(self.market.pairs)

            snapshot = await self.portfolio_mgr.get_snapshot(prices)

            self.learning.backfill_from_decisions()
            self.learning.evaluate_pending(prices)
            learning_summary = self.learning.build_learning_summary()

            use_mcp = bool(self.ai.settings.cursor_api_key)
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
                derivatives_summary=self.derivatives.summarize(deriv_snapshots),
                market_status="crypto-only",
                use_mcp=use_mcp,
                profile=self.profile,
            )

            if self.profile == DEFAULT_PROFILE:
                _last_market_context = {
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

            decision, raw_response, run_id, token_usage = await self.ai.run_cycle(prompt)

            approved_count = 0
            rejected_count = 0
            approved_symbols: set[str] = set()
            rejected_proposals: list[tuple] = []

            for proposal in decision.proposals:
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
                if deriv:
                    funding_rate = deriv.funding_rate

                check = self.risk.validate_proposal(
                    proposal,
                    snapshot,
                    price,
                    cycle_id,
                    fee_rate=fee_rate,
                    quant_conviction=quant_map.get(proposal.symbol),
                )
                if check.approved:
                    success = await self.executor.execute_approved(
                        check, price, cycle_id, funding_rate=funding_rate
                    )
                    if success:
                        approved_count += 1
                        approved_symbols.add(proposal.symbol)
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
                approved_symbols=approved_symbols,
                rejected=rejected_proposals,
            )

            ai_record = AIDecision(
                cycle_id=cycle_id,
                profile=self.profile,
                model=self.ai.settings.ai_model,
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
        }

    def _format_portfolio(self, snapshot) -> str:
        label = PROFILE_LABELS.get(self.profile, self.profile)
        lines = [
            f"Profile: {label} ({snapshot.profile})",
            f"Mode: {snapshot.mode.value}",
            f"Quote balance: {snapshot.quote_balance:.2f} USDT",
            f"Total value: {snapshot.total_value_usdt:.2f} USDT",
            f"Unrealized P&L: {snapshot.unrealized_pnl:.2f}",
            f"Realized P&L: {snapshot.realized_pnl:.2f}",
            f"Kill switch: {snapshot.kill_switch_active}",
        ]
        for pos in snapshot.positions:
            inst = getattr(pos, "instrument_type", "spot")
            lines.append(
                f"  {pos.symbol} [{inst}]: {pos.amount:.6f} @ {pos.entry_price:.2f} "
                f"(now {pos.current_price:.2f}, PnL {pos.unrealized_pnl:.2f})"
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
