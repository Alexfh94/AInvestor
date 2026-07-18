from __future__ import annotations

import logging
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

import httpx

from ainvestor.config import get_all_market_pairs
from ainvestor.models.schemas import DerivativesSnapshot

logger = logging.getLogger(__name__)

# Binance.com futures blocks US cloud IPs (e.g. GCP us-east1).
# OKX works from US; Bybit is geo-blocked there — keep both as fallbacks.
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BYBIT_TICKER_URL = "https://api.bybit.com/v5/market/tickers"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
OKX_OI_URL = "https://www.okx.com/api/v5/public/open-interest"
OKX_MARK_URL = "https://www.okx.com/api/v5/public/mark-price"

DERIVATIVES_FALLBACK_SOURCES = ("okx", "bybit")


class DerivativesCollector:
    """Funding rate and open interest from Binance futures with exchange fallbacks."""

    def __init__(self):
        self._pairs = get_all_market_pairs()

    def _perp_symbol(self, spot_symbol: str) -> str:
        base = spot_symbol.split("/")[0]
        return f"{base}USDT"

    def _okx_inst_id(self, spot_symbol: str) -> str:
        base = spot_symbol.split("/")[0]
        return f"{base}-USDT-SWAP"

    @staticmethod
    def _parse_ms_timestamp(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromtimestamp(int(value) / 1000)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _snapshot(
        symbol: str,
        *,
        funding_rate: float,
        mark_price: float,
        open_interest: float,
        next_funding_time: datetime | None = None,
    ) -> DerivativesSnapshot:
        return DerivativesSnapshot(
            symbol=symbol,
            funding_rate=funding_rate,
            funding_rate_pct=funding_rate * 100,
            mark_price=mark_price,
            open_interest=open_interest,
            next_funding_time=next_funding_time,
            timestamp=app_now(),
        )

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
        try:
            return await self._collect_from_binance(client, symbol)
        except Exception as primary_err:
            for source in DERIVATIVES_FALLBACK_SOURCES:
                try:
                    snap = await self._collect_from_source(client, symbol, source)
                    if snap:
                        logger.info(
                            "Using %s fallback for derivatives %s (binance blocked/unavailable)",
                            source,
                            symbol,
                        )
                        return snap
                except Exception as fb_err:
                    logger.debug(
                        "Derivatives fallback %s failed for %s: %s",
                        source,
                        symbol,
                        fb_err,
                    )
            raise primary_err

    async def _collect_from_binance(
        self, client: httpx.AsyncClient, symbol: str
    ) -> DerivativesSnapshot:
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
        next_funding = self._parse_ms_timestamp(funding_data.get("nextFundingTime"))

        return self._snapshot(
            symbol,
            funding_rate=rate,
            mark_price=mark,
            open_interest=oi,
            next_funding_time=next_funding,
        )

    async def _collect_from_source(
        self, client: httpx.AsyncClient, symbol: str, source: str
    ) -> DerivativesSnapshot:
        if source == "bybit":
            return await self._collect_from_bybit(client, symbol)
        if source == "okx":
            return await self._collect_from_okx(client, symbol)
        raise ValueError(f"Unknown derivatives source: {source}")

    async def _collect_from_bybit(
        self, client: httpx.AsyncClient, symbol: str
    ) -> DerivativesSnapshot:
        perp = self._perp_symbol(symbol)
        resp = await client.get(
            BYBIT_TICKER_URL, params={"category": "linear", "symbol": perp}
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(payload.get("retMsg") or "Bybit derivatives error")

        rows = payload.get("result", {}).get("list") or []
        if not rows:
            raise RuntimeError(f"Bybit returned no ticker for {perp}")

        row = rows[0]
        return self._snapshot(
            symbol,
            funding_rate=float(row.get("fundingRate") or 0),
            mark_price=float(row.get("markPrice") or 0),
            open_interest=float(row.get("openInterest") or 0),
            next_funding_time=self._parse_ms_timestamp(row.get("nextFundingTime")),
        )

    async def _collect_from_okx(
        self, client: httpx.AsyncClient, symbol: str
    ) -> DerivativesSnapshot:
        inst_id = self._okx_inst_id(symbol)

        funding_resp = await client.get(OKX_FUNDING_URL, params={"instId": inst_id})
        funding_resp.raise_for_status()
        funding_payload = funding_resp.json()
        if funding_payload.get("code") != "0":
            raise RuntimeError(funding_payload.get("msg") or "OKX funding error")
        funding_rows = funding_payload.get("data") or []
        if not funding_rows:
            raise RuntimeError(f"OKX returned no funding for {inst_id}")
        funding_row = funding_rows[0]

        oi_resp = await client.get(
            OKX_OI_URL, params={"instType": "SWAP", "instId": inst_id}
        )
        oi_resp.raise_for_status()
        oi_payload = oi_resp.json()
        if oi_payload.get("code") != "0":
            raise RuntimeError(oi_payload.get("msg") or "OKX open interest error")
        oi_rows = oi_payload.get("data") or []
        if not oi_rows:
            raise RuntimeError(f"OKX returned no OI for {inst_id}")

        ticker_resp = await client.get(
            OKX_MARK_URL, params={"instType": "SWAP", "instId": inst_id}
        )
        ticker_resp.raise_for_status()
        ticker_payload = ticker_resp.json()
        if ticker_payload.get("code") != "0":
            raise RuntimeError(ticker_payload.get("msg") or "OKX mark price error")
        ticker_rows = ticker_payload.get("data") or []
        mark_price = float(ticker_rows[0].get("markPx") or 0) if ticker_rows else 0.0

        return self._snapshot(
            symbol,
            funding_rate=float(funding_row.get("fundingRate") or 0),
            mark_price=mark_price,
            open_interest=float(oi_rows[0].get("oi") or 0),
            next_funding_time=self._parse_ms_timestamp(
                funding_row.get("nextFundingTime")
            ),
        )

    def summarize(
        self,
        snapshots: list[DerivativesSnapshot],
        spot_prices: dict[str, float] | None = None,
    ) -> str:
        if not snapshots:
            return "No derivatives data available."
        lines = []
        for s in sorted(snapshots, key=lambda x: abs(x.funding_rate_pct), reverse=True):
            bias = "longs pay" if s.funding_rate > 0 else "shorts pay"
            basis_str = ""
            if spot_prices:
                spot = spot_prices.get(s.symbol, 0)
                if spot > 0 and s.mark_price > 0:
                    basis = (s.mark_price - spot) / spot * 100
                    basis_str = f", basis={basis:+.3f}%"
            nft = ""
            if s.next_funding_time:
                nft = f", next_funding={s.next_funding_time.strftime('%H:%M')}"
            lines.append(
                f"{s.symbol}: funding {s.funding_rate_pct:+.4f}% ({bias}){basis_str}, "
                f"OI={s.open_interest:,.0f}, mark={s.mark_price:.4f}{nft}"
            )
        return "\n".join(lines)
