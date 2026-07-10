from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

import httpx

from ainvestor.config import get_all_market_pairs, load_risk_config
from ainvestor.models.schemas import DerivativesSnapshot

logger = logging.getLogger(__name__)

BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"


class DerivativesCollector:
    """Funding rate and open interest from Binance futures public API."""

    def __init__(self):
        self._pairs = get_all_market_pairs()

    def _perp_symbol(self, spot_symbol: str) -> str:
        base = spot_symbol.split("/")[0]
        return f"{base}USDT"

    async def collect(self) -> list[DerivativesSnapshot]:
        snapshots: list[DerivativesSnapshot] = []
        async with httpx.AsyncClient(timeout=20) as client:
            for symbol in self._pairs:
                try:
                    snap = await self._collect_symbol(client, symbol)
                    if snap:
                        snapshots.append(snap)
                except Exception as e:
                    logger.warning("Derivatives fetch failed for %s: %s", symbol, e)
        return snapshots

    async def _collect_symbol(
        self, client: httpx.AsyncClient, symbol: str
    ) -> DerivativesSnapshot | None:
        perp = self._perp_symbol(symbol)
        funding_resp = await client.get(BINANCE_FUNDING_URL, params={"symbol": perp})
        funding_resp.raise_for_status()
        funding_data = funding_resp.json()

        oi_resp = await client.get(BINANCE_OI_URL, params={"symbol": perp})
        oi_resp.raise_for_status()
        oi_data = oi_resp.json()

        rate = float(funding_data.get("lastFundingRate") or 0)
        mark = float(funding_data.get("markPrice") or 0)
        oi = float(oi_data.get("openInterest") or 0)

        return DerivativesSnapshot(
            symbol=symbol,
            funding_rate=rate,
            funding_rate_pct=rate * 100,
            mark_price=mark,
            open_interest=oi,
            timestamp=app_now(),
        )

    def summarize(self, snapshots: list[DerivativesSnapshot]) -> str:
        if not snapshots:
            return "No derivatives data available."
        lines = []
        for s in sorted(snapshots, key=lambda x: abs(x.funding_rate_pct), reverse=True)[:10]:
            bias = "longs pay" if s.funding_rate > 0 else "shorts pay"
            lines.append(
                f"{s.symbol}: funding {s.funding_rate_pct:+.4f}% ({bias}), "
                f"OI={s.open_interest:,.0f}, mark={s.mark_price:.4f}"
            )
        return "\n".join(lines)
