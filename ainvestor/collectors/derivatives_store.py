from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ainvestor.config import load_risk_config
from ainvestor.db.models import DerivativesRecord
from ainvestor.models.schemas import DerivativesSnapshot

logger = logging.getLogger(__name__)

from ainvestor.collectors.derivatives import DerivativesCollector as _BaseDerivativesCollector


class DerivativesCollector(_BaseDerivativesCollector):
    """Extends derivatives collector with DB persistence."""

    def __init__(self, db: Session | None = None):
        super().__init__()
        self.db = db

    async def collect_and_persist(self) -> list[DerivativesSnapshot]:
        snapshots = await self.collect()
        if self.db is not None:
            for s in snapshots:
                self.db.add(
                    DerivativesRecord(
                        symbol=s.symbol,
                        funding_rate=s.funding_rate,
                        funding_rate_pct=s.funding_rate_pct,
                        mark_price=s.mark_price,
                        open_interest=s.open_interest,
                        captured_at=s.timestamp,
                    )
                )
            self.db.commit()
        return snapshots
