from __future__ import annotations

import logging
from typing import Any

from ainvestor.config import get_settings, load_risk_config

logger = logging.getLogger(__name__)


class IBKRBroker:
    """Interactive Brokers adapter via ib_insync (paper or live)."""

    def __init__(self):
        self.settings = get_settings()
        self.risk = load_risk_config().get("ibkr", {})
        self._ib = None
        self._connected = False

    @property
    def enabled(self) -> bool:
        return bool(self.risk.get("enabled")) and self.settings.stock_trading_mode.startswith("ibkr")

    async def connect(self) -> bool:
        if not self.enabled:
            return False
        try:
            from ib_insync import IB

            self._ib = IB()
            host = self.risk.get("host", "127.0.0.1")
            port = int(self.risk.get("port", 7497))
            client_id = int(self.risk.get("client_id", 1))
            await self._ib.connectAsync(host, port, clientId=client_id)
            self._connected = True
            logger.info("IBKR connected %s:%s", host, port)
            return True
        except ImportError:
            logger.warning("ib_insync not installed — pip install ainvestor[ibkr]")
            return False
        except Exception as e:
            logger.error("IBKR connect failed: %s", e)
            return False

    async def disconnect(self) -> None:
        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self._connected or self._ib is None:
            return []
        positions = self._ib.positions()
        return [
            {
                "symbol": p.contract.symbol,
                "amount": p.position,
                "avg_cost": p.avgCost,
                "account": p.account,
            }
            for p in positions
        ]

    async def get_account_summary(self) -> dict[str, float]:
        if not self._connected or self._ib is None:
            return {}
        summary = self._ib.accountSummary()
        result: dict[str, float] = {}
        for item in summary:
            if item.tag in ("NetLiquidation", "TotalCashValue", "AvailableFunds"):
                try:
                    result[item.tag] = float(item.value)
                except ValueError:
                    pass
        return result

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> dict[str, Any] | None:
        if not self._connected or self._ib is None:
            return None
        try:
            from ib_insync import MarketOrder, Stock

            contract = Stock(symbol, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            order = MarketOrder("BUY" if side == "buy" else "SELL", quantity)
            trade = self._ib.placeOrder(contract, order)
            await trade.filledEvent
            return {
                "order_id": trade.order.orderId,
                "status": trade.orderStatus.status,
                "filled": trade.orderStatus.filled,
                "avg_price": trade.orderStatus.avgFillPrice,
            }
        except Exception as e:
            logger.error("IBKR order failed %s: %s", symbol, e)
            return None

    async def sync_positions_to_db(self, db) -> int:
        """Sync broker positions — stub for manual reconciliation."""
        positions = await self.get_positions()
        logger.info("IBKR sync: %d positions", len(positions))
        return len(positions)
