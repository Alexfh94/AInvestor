from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ainvestor.config import get_settings, load_risk_config
from ainvestor.models.schemas import TradingMode, UnifiedPortfolioSnapshot
from ainvestor.portfolio.manager import PortfolioManager
from ainvestor.portfolio.stock_simulator import StockPortfolioManager
from ainvestor.services.fx import FXConverter

logger = logging.getLogger(__name__)


class UnifiedPortfolioManager:
    """Aggregated portfolio view across crypto, stocks and perps in EUR."""

    def __init__(self, db: Session):
        self.db = db
        self.crypto_mgr = PortfolioManager(db)
        self.stock_mgr = StockPortfolioManager(db)
        self.fx = FXConverter()
        self.settings = get_settings()

    async def get_snapshot(
        self,
        crypto_prices: dict[str, float],
        stock_prices: dict[str, float] | None = None,
    ) -> UnifiedPortfolioSnapshot:
        crypto_snap = await self.crypto_mgr.get_snapshot(crypto_prices)
        usdt_eur = await self.fx.usdt_to_eur()
        usd_eur = await self.fx.usd_to_eur()

        crypto_value_eur = crypto_snap.total_value_usdt * usdt_eur

        stock_value_eur = 0.0
        cash_eur = 0.0
        stock_portfolio = self.stock_mgr.get_or_create()
        cash_eur += stock_portfolio.cash_eur
        cash_eur += stock_portfolio.cash_usd * usd_eur

        stock_prices = stock_prices or {}
        for pos in self.stock_mgr.get_simulator().get_open_positions():
            price = stock_prices.get(pos.symbol, pos.entry_price)
            stock_value_eur += pos.shares * price * usd_eur

        perp_margin_eur = 0.0
        for pos in crypto_snap.positions:
            if hasattr(pos, "symbol"):
                db_pos = next(
                    (p for p in self.crypto_mgr.get_simulator().get_open_positions() if p.symbol == pos.symbol),
                    None,
                )
                if db_pos and getattr(db_pos, "instrument_type", "spot") == "perpetual":
                    margin = getattr(db_pos, "margin_used", 0) or 0
                    perp_margin_eur += margin * usdt_eur

        total = crypto_value_eur + stock_value_eur + cash_eur

        return UnifiedPortfolioSnapshot(
            total_equity_eur=total,
            crypto_value_eur=crypto_value_eur,
            stock_value_eur=stock_value_eur,
            perp_margin_eur=perp_margin_eur,
            cash_eur=cash_eur,
            mode=TradingMode(self.settings.trading_mode),
            kill_switch_active=crypto_snap.kill_switch_active,
        )

    def check_allocation_limits(self, allocation: dict[str, float]) -> list[str]:
        """Validate AI allocation proposal against risk.yaml caps."""
        reasons: list[str] = []
        alloc_cfg = load_risk_config().get("allocation", {})
        for key, cap_key in (
            ("crypto", "max_crypto_pct"),
            ("stocks", "max_stocks_pct"),
            ("derivatives", "max_derivatives_pct"),
        ):
            pct = allocation.get(key, 0)
            cap = float(alloc_cfg.get(cap_key, 100))
            if pct > cap:
                reasons.append(f"Allocation {key} {pct}% exceeds cap {cap}%")
        return reasons
