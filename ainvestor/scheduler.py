from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ainvestor.config import get_profile_ai_cycle_interval, get_settings
from ainvestor.cycle_runner import CycleRunner
from ainvestor.db.models import SessionLocal
from ainvestor.portfolio.profiles import PROFILES

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _run_ai_cycle_for_profile(profile: str):
    db = SessionLocal()
    try:
        runner = CycleRunner(db, profile=profile)
        result = await runner.run()
        logger.info("AI cycle completed (%s): %s", profile, result)
    except Exception as e:
        logger.exception("AI cycle error (%s): %s", profile, e)
    finally:
        db.close()


async def _run_risk_monitor():
    db = SessionLocal()
    try:
        for profile in PROFILES:
            runner = CycleRunner(db, profile=profile)
            result = await runner.run_risk_monitor()
            if result.get("kill_switch") or result.get("stop_triggers"):
                logger.warning("Risk monitor alert (%s): %s", profile, result)
    except Exception as e:
        logger.exception("Risk monitor error: %s", e)
    finally:
        db.close()


async def _run_market_collect():
    db = SessionLocal()
    try:
        from ainvestor.collectors.market import MarketCollector
        from ainvestor.portfolio.manager import PortfolioManager
        from ainvestor.services.charts import record_portfolio_value_async

        collector = MarketCollector(db)
        tickers = await collector.collect_all()
        logger.info("Collected %d market snapshots", len(tickers))

        prices = {t.symbol: t.last for t in tickers}
        for profile in PROFILES:
            mgr = PortfolioManager(db, profile=profile)
            await record_portfolio_value_async(db, mgr, prices)
    except Exception as e:
        logger.exception("Market collect error: %s", e)
    finally:
        db.close()


async def _run_learning_eval_for_profile(profile: str):
    db = SessionLocal()
    try:
        from ainvestor.collectors.market import MarketCollector
        from ainvestor.engine.learning import DecisionLearning

        collector = MarketCollector(db)
        prices: dict[str, float] = {}
        for symbol in collector.pairs:
            try:
                ticker = await collector.client.fetch_ticker(symbol)
                prices[symbol] = ticker.get("last") or ticker.get("close", 0)
            except Exception:
                pass

        learning = DecisionLearning(db, profile=profile)
        learning.backfill_from_decisions()
        count = learning.evaluate_pending(prices)
        if count:
            logger.info("Learning evaluation (%s): %d outcomes updated", profile, count)
    except Exception as e:
        logger.exception("Learning eval error (%s): %s", profile, e)
    finally:
        db.close()


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    _scheduler = AsyncIOScheduler()

    ai_intervals: dict[str, int] = {}
    for profile in PROFILES:
        interval = get_profile_ai_cycle_interval(profile)
        ai_intervals[profile] = interval
        _scheduler.add_job(
            _run_ai_cycle_for_profile,
            IntervalTrigger(minutes=interval),
            id=f"ai_cycle_{profile}",
            kwargs={"profile": profile},
            replace_existing=True,
        )
        _scheduler.add_job(
            _run_learning_eval_for_profile,
            IntervalTrigger(minutes=interval),
            id=f"learning_eval_{profile}",
            kwargs={"profile": profile},
            replace_existing=True,
        )

    _scheduler.add_job(
        _run_risk_monitor,
        IntervalTrigger(minutes=settings.risk_monitor_interval),
        id="risk_monitor",
        replace_existing=True,
    )
    _scheduler.add_job(
        _run_market_collect,
        IntervalTrigger(minutes=settings.market_collect_interval),
        id="market_collect",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started: AI cycles=%s, Risk=%dmin, Market=%dmin",
        ai_intervals,
        settings.risk_monitor_interval,
        settings.market_collect_interval,
    )
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
