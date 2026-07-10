#!/usr/bin/env python3
"""Backtest strategy using historical OHLCV data."""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.engine.quant import QuantEngine
from ainvestor.models.schemas import DecisionAction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BacktestResult:
    def __init__(self):
        self.trades: list[dict] = []
        self.equity_curve: list[float] = []
        self.initial_capital = 10000.0
        self.capital = 10000.0
        self.positions: dict[str, dict] = {}

    def buy(self, symbol: str, price: float, amount_pct: float, timestamp: int):
        amount_usdt = self.capital * (amount_pct / 100)
        if amount_usdt < 10 or self.capital < amount_usdt:
            return
        fee = amount_usdt * 0.001
        amount_base = (amount_usdt - fee) / price
        self.capital -= amount_usdt
        self.positions[symbol] = {"amount": amount_base, "entry": price}
        self.trades.append({"side": "buy", "symbol": symbol, "price": price, "ts": timestamp})

    def sell(self, symbol: str, price: float, timestamp: int):
        pos = self.positions.get(symbol)
        if not pos:
            return
        value = pos["amount"] * price
        fee = value * 0.001
        self.capital += value - fee
        pnl = value - pos["amount"] * pos["entry"]
        self.trades.append({"side": "sell", "symbol": symbol, "price": price, "pnl": pnl, "ts": timestamp})
        del self.positions[symbol]

    def mark_to_market(self, prices: dict[str, float]):
        total = self.capital
        for sym, pos in self.positions.items():
            total += pos["amount"] * prices.get(sym, pos["entry"])
        self.equity_curve.append(total)
        return total

    def metrics(self) -> dict:
        if not self.equity_curve:
            return {}
        equity = np.array(self.equity_curve)
        returns = np.diff(equity) / equity[:-1]
        returns = returns[np.isfinite(returns)]

        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak
        max_dd = float(np.max(drawdown)) if len(drawdown) else 0

        sells = [t for t in self.trades if t["side"] == "sell"]
        wins = [t for t in sells if t.get("pnl", 0) > 0]
        losses = [t for t in sells if t.get("pnl", 0) <= 0]
        gross_profit = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losses))

        sharpe = 0.0
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(365 * 24))

        return {
            "initial_capital": self.initial_capital,
            "final_value": float(equity[-1]),
            "total_return_pct": float((equity[-1] / self.initial_capital - 1) * 100),
            "max_drawdown_pct": float(max_dd * 100),
            "sharpe_ratio": round(sharpe, 3),
            "total_trades": len(self.trades),
            "win_rate": len(wins) / len(sells) * 100 if sells else 0,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        }


async def run_backtest(symbols: list[str], days: int = 180) -> BacktestResult:
    client = ExchangeClient()
    quant = QuantEngine()
    result = BacktestResult()

    for symbol in symbols:
        logger.info("Backtesting %s...", symbol)
        ohlcv = await client.fetch_ohlcv(symbol, timeframe="1h", limit=min(days * 24, 1000))
        if len(ohlcv) < 50:
            logger.warning("Insufficient data for %s", symbol)
            continue

        position_open = False
        for i in range(30, len(ohlcv)):
            window = ohlcv[: i + 1]
            signal = quant.analyze(symbol, window)
            price = ohlcv[i][4]
            ts = ohlcv[i][0]

            if not position_open and signal.trend == "bullish" and signal.conviction_score >= 65:
                result.buy(symbol, price, 10.0, ts)
                position_open = True
            elif position_open and (signal.trend == "bearish" or signal.rsi and signal.rsi > 70):
                result.sell(symbol, price, ts)
                position_open = False

            result.mark_to_market({symbol: price})

    return result


async def main():
    parser = argparse.ArgumentParser(description="AInvestor backtest")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT", help="Comma-separated pairs")
    parser.add_argument("--days", type=int, default=90, help="Days of history")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    result = await run_backtest(symbols, args.days)
    metrics = result.metrics()

    print("\n=== Backtest Results ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    btc_client = ExchangeClient()
    btc_ohlcv = await btc_client.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=min(args.days * 24, 1000))
    if btc_ohlcv:
        btc_return = (btc_ohlcv[-1][4] / btc_ohlcv[0][4] - 1) * 100
        print(f"\n  BTC buy-and-hold return: {btc_return:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
