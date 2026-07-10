"""Binance testnet connector configuration and helpers."""

from __future__ import annotations

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import get_settings

TESTNET_DOCS = "https://testnet.binance.vision/"


def get_testnet_client() -> ExchangeClient:
    """Return ccxt client configured for Binance spot testnet."""
    return ExchangeClient(exchange_id="binance", testnet=True)


def validate_testnet_credentials() -> dict:
    """Check if testnet API keys are configured."""
    settings = get_settings()
    return {
        "configured": bool(settings.binance_api_key and settings.binance_api_secret),
        "docs": TESTNET_DOCS,
        "note": "Set BINANCE_API_KEY and BINANCE_API_SECRET from testnet.binance.vision",
    }
