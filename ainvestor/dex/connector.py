"""Future: DEX on-chain connector for altcoins not listed on CEX.

This module is a placeholder for Phase 8. When implemented, it will:
- Read wallet balances via web3.py
- Detect tokens outside CEX whitelist
- Report opportunities without executing (v2.1)
- Execute swaps via Uniswap/PancakeSwap router (v2.2)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DexOpportunity:
    token_address: str
    symbol: str
    reason: str
    detected_at: str


class DexConnector:
    """Placeholder for on-chain DEX integration."""

    def __init__(self, rpc_url: str | None = None, wallet_address: str | None = None):
        self.rpc_url = rpc_url
        self.wallet_address = wallet_address
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled and bool(self.rpc_url and self.wallet_address)

    async def detect_cex_gaps(self, cex_symbols: list[str]) -> list[DexOpportunity]:
        """Detect trending tokens not available on configured CEX pairs."""
        logger.info("DEX connector not yet enabled - returning empty opportunities")
        return []

    async def get_wallet_balances(self) -> dict[str, float]:
        if not self.is_enabled:
            return {}
        raise NotImplementedError("DEX wallet reading planned for Phase 8")

    async def execute_swap(self, token_in: str, token_out: str, amount: float) -> dict:
        raise NotImplementedError("DEX execution planned for Phase 8")
