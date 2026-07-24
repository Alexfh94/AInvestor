"""Technical signal collection shared by AI cycles, dashboard and market jobs."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ainvestor.collectors.market import MarketCollector
from ainvestor.engine.quant import QuantEngine
from ainvestor.models.schemas import TechnicalSignal

logger = logging.getLogger(__name__)


async def collect_technical_signals(db: Session) -> list[TechnicalSignal]:
    """Fetch multi-TF OHLCV and run quant analysis."""
    market = MarketCollector(db)
    try:
        mtf_data = await market.collect_all_multi_timeframe()
        if not mtf_data:
            logger.warning("No multi-timeframe data for signal computation")
            return []
        return QuantEngine().analyze_all_multi(mtf_data)
    except Exception as exc:
        logger.warning("Technical signal collection failed: %s", exc)
        return []


def signals_to_dicts(signals: list[TechnicalSignal]) -> list[dict]:
    return [s.model_dump() for s in signals]
