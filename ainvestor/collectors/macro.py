from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

import httpx

from ainvestor.models.schemas import MacroContext

logger = logging.getLogger(__name__)

COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"


class MacroCollector:
    """BTC dominance and global crypto market context."""

    async def collect(self) -> MacroContext:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(COINGECKO_GLOBAL_URL)
                resp.raise_for_status()
                data = resp.json().get("data", {})
            return MacroContext(
                btc_dominance=float(data.get("market_cap_percentage", {}).get("btc", 0)),
                total_market_cap_usd=float(data.get("total_market_cap", {}).get("usd", 0)),
                market_cap_change_24h_pct=float(
                    data.get("market_cap_change_percentage_24h_usd", 0)
                ),
                timestamp=app_now(),
            )
        except Exception as e:
            logger.warning("Macro context fetch failed: %s", e)
            return MacroContext(timestamp=app_now())

    def summarize(self, ctx: MacroContext) -> str:
        if ctx.btc_dominance is None or ctx.btc_dominance <= 0:
            return "No macro context available."
        lines = [
            f"BTC dominance: {ctx.btc_dominance:.1f}%",
            f"Total crypto market cap: ${ctx.total_market_cap_usd:,.0f}",
            f"Market cap 24h change: {ctx.market_cap_change_24h_pct:+.2f}%",
        ]
        return "\n".join(lines)
