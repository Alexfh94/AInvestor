from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

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
from ainvestor.portfolio.manager import PortfolioManager

logger = logging.getLogger(__name__)


class CycleRunner:
    """Orchestrates a full AI trading cycle."""

    def __init__(self, db: Session):
        self.db = db
        self.market = MarketCollector(db)
        self.news = NewsCollector()
        self.sentiment = SentimentCollector()
        self.quant = QuantEngine()
        self.portfolio_mgr = PortfolioManager(db)
        self.risk = RiskManager(db)
        self.executor = TradeExecutor(db)
        self.ai = AIAgent()
        self.learning = DecisionLearning(db)

    async def run(self, cycle_id: str | None = None) -> dict:
        cycle_id = cycle_id or PortfolioManager.new_cycle_id()

        cycle_run = CycleRun(cycle_id=cycle_id, status="running")
        self.db.add(cycle_run)
        self.db.commit()

        try:
            tickers = await self.market.collect_all()
            prices = {t.symbol: t.last for t in tickers}

            ohlcv_data: dict[str, list] = {}
            for symbol in self.market.pairs:
                try:
                    ohlcv_data[symbol] = await self.market.get_latest_ohlcv(symbol)
                except Exception as e:
                    logger.warning("OHLCV failed for %s: %s", symbol, e)

            signals = self.quant.analyze_all(ohlcv_data)
            news_items = await self.news.collect()
            sentiment_data = await self.sentiment.collect()

            from ainvestor.dex import DexConnector

            dex = DexConnector()
            dex_gaps = await dex.detect_cex_gaps(self.market.pairs)

            snapshot = await self.portfolio_mgr.get_snapshot(prices)
            risk_config = load_risk_config()

            self.learning.backfill_from_decisions()
            self.learning.evaluate_pending(prices)
            learning_summary = self.learning.build_learning_summary()

            prompt = build_cycle_prompt(
                portfolio_summary=self._format_portfolio(snapshot),
                market_summary=self._format_market(tickers),
                signals_summary=self.quant.summarize(signals),
                news_summary=self.news.summarize(news_items),
                sentiment_summary=self.sentiment.summarize(sentiment_data),
                risk_config=risk_config,
                learning_summary=learning_summary,
            )

            decision, raw_response, run_id, token_usage = await self.ai.run_cycle(prompt)

            approved_count = 0
            rejected_count = 0
            approved_symbols: set[str] = set()
            rejected_proposals: list[tuple] = []

            for proposal in decision.proposals:
                price = prices.get(proposal.symbol, 0)
                if price <= 0:
                    rejected_count += 1
                    rejected_proposals.append((proposal, ["Precio no disponible"]))
                    continue

                fee_rate = await self.market.client.get_taker_fee_rate(proposal.symbol)
                check = self.risk.validate_proposal(
                    proposal, snapshot, price, cycle_id, fee_rate=fee_rate
                )
                if check.approved:
                    success = await self.executor.execute_approved(check, price, cycle_id)
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
            cycle_run.completed_at = datetime.utcnow()
            self.db.commit()

            return {
                "cycle_id": cycle_id,
                "status": "completed",
                "hold": decision.hold,
                "summary": decision.summary,
                "proposals": len(decision.proposals),
                "approved": approved_count,
                "rejected": rejected_count,
                "run_id": run_id,
                "token_usage": token_usage.to_dict(),
            }

        except Exception as e:
            logger.exception("Cycle %s failed: %s", cycle_id, e)
            cycle_run.status = "error"
            cycle_run.error = str(e)
            cycle_run.completed_at = datetime.utcnow()
            self.db.commit()
            return {"cycle_id": cycle_id, "status": "error", "error": str(e)}

    async def run_risk_monitor(self) -> dict:
        """5-minute risk monitor: stop-loss, take-profit, kill switch."""
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
            logger.warning("Kill switch activated due to max drawdown")
            from ainvestor.alerts import send_telegram_alert

            await send_telegram_alert("AInvestor: Kill switch activated (max drawdown)")
            return {"kill_switch": True, "reason": "max_drawdown"}

        triggers = self.risk.check_stop_loss_take_profit(snapshot)
        executed = []
        for symbol, action, price in triggers:
            if action == "sell":
                success = await self.executor.execute_stop_trigger(symbol, price)
                if success:
                    executed.append(symbol)

        from ainvestor.services.charts import record_portfolio_value_async

        await record_portfolio_value_async(self.db, self.portfolio_mgr, prices)

        return {"kill_switch": snapshot.kill_switch_active, "stop_triggers": executed}

    def _format_portfolio(self, snapshot) -> str:
        lines = [
            f"Mode: {snapshot.mode.value}",
            f"Quote balance: {snapshot.quote_balance:.2f} USDT",
            f"Total value: {snapshot.total_value_usdt:.2f} USDT",
            f"Unrealized P&L: {snapshot.unrealized_pnl:.2f}",
            f"Realized P&L: {snapshot.realized_pnl:.2f}",
            f"Kill switch: {snapshot.kill_switch_active}",
        ]
        for pos in snapshot.positions:
            lines.append(
                f"  {pos.symbol}: {pos.amount:.6f} @ {pos.entry_price:.2f} "
                f"(now {pos.current_price:.2f}, PnL {pos.unrealized_pnl:.2f})"
            )
        return "\n".join(lines)

    def _format_market(self, tickers) -> str:
        sorted_tickers = sorted(tickers, key=lambda t: abs(t.change_pct or 0), reverse=True)
        lines = []
        for t in sorted_tickers[:10]:
            chg = f"{t.change_pct:+.2f}%" if t.change_pct else "N/A"
            lines.append(f"{t.symbol}: {t.last:.4f} ({chg})")
        return "\n".join(lines)
