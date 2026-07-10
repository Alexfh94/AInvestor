from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

ECB_USD_EUR_URL = "https://api.frankfurter.app/latest?from=USD&to=EUR"
COINGECKO_USDT_URL = (
    "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=eur,usd"
)

_cached_usd_eur: float | None = None
_cached_usdt_eur: float | None = None


class FXConverter:
    """Convert between USD, EUR and USDT for unified portfolio view."""

    async def usd_to_eur(self) -> float:
        global _cached_usd_eur
        if _cached_usd_eur is not None:
            return _cached_usd_eur
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(ECB_USD_EUR_URL)
                resp.raise_for_status()
                rate = float(resp.json()["rates"]["EUR"])
                _cached_usd_eur = rate
                return rate
        except Exception as e:
            logger.warning("USD/EUR fetch failed: %s — using 0.92", e)
            return 0.92

    async def usdt_to_eur(self) -> float:
        global _cached_usdt_eur
        if _cached_usdt_eur is not None:
            return _cached_usdt_eur
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(COINGECKO_USDT_URL)
                resp.raise_for_status()
                rate = float(resp.json()["tether"]["eur"])
                _cached_usdt_eur = rate
                return rate
        except Exception as e:
            logger.warning("USDT/EUR fetch failed: %s — using USD/EUR", e)
            return await self.usd_to_eur()

    async def convert_to_eur(self, amount: float, currency: str) -> float:
        currency = currency.upper()
        if currency == "EUR":
            return amount
        if currency == "USD":
            return amount * await self.usd_to_eur()
        if currency in ("USDT", "USDC"):
            return amount * await self.usdt_to_eur()
        return amount
