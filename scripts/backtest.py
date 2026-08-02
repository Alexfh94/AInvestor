#!/usr/bin/env python3
"""Backtest extreme profile: perps 20x all-in with SignalEngine."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field

import numpy as np

from ainvestor.collectors.exchange_client import ExchangeClient
from ainvestor.config import load_risk_config
from ainvestor.engine.quant import QuantEngine
from ainvestor.engine.signal_engine import SignalEngine
from ainvestor.models.schemas import (
    DerivativesSnapshot,
    PortfolioSnapshot,
    TradingMode,
)
from ainvestor.utils.datetime_utils import app_now

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class BacktestPosition:
    symbol: str
    side: str
    entry_price: float
    margin: float
    notional: float
    leverage: int
    amount: float
    opened_at: int


@dataclass
class BacktestResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    initial_capital: float = 100.0
    capital: float = 100.0
    position: BacktestPosition | None = None

    def open_position(
        self,
        symbol: str,
        side: str,
        price: float,
        leverage: int,
        fee_rate: float,
        timestamp: int,
    ) -> bool:
        if self.position is not None or self.capital <= 0:
            return False
        reserve = 0.001
        usable = self.capital * (1 - reserve)
        margin = usable / (1 + leverage * fee_rate)
        fee = margin * leverage * fee_rate
        notional = margin * leverage
        amount = notional / price
        self.capital -= margin + fee
        self.position = BacktestPosition(
            symbol=symbol,
            side=side,
            entry_price=price,
            margin=margin,
            notional=notional,
            leverage=leverage,
            amount=amount,
            opened_at=timestamp,
        )
        self.trades.append(
            {
                "action": "open",
                "symbol": symbol,
                "side": side,
                "price": price,
                "margin": margin,
                "ts": timestamp,
            }
        )
        return True

    def close_position(self, price: float, fee_rate: float, timestamp: int, reason: str) -> float:
        pos = self.position
        if pos is None:
            return 0.0
        if pos.side == "long":
            pnl = (price - pos.entry_price) * pos.amount
        else:
            pnl = (pos.entry_price - price) * pos.amount
        close_fee = pos.notional * fee_rate
        roe = (pnl / pos.margin) * 100 if pos.margin else 0.0
        self.capital += pos.margin + pnl - close_fee
        self.trades.append(
            {
                "action": "close",
                "symbol": pos.symbol,
                "side": pos.side,
                "price": price,
                "pnl": pnl - close_fee,
                "roe": roe,
                "reason": reason,
                "ts": timestamp,
            }
        )
        self.position = None
        return pnl - close_fee

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        pos = self.position
        if pos is None:
            return 0.0
        price = prices.get(pos.symbol, pos.entry_price)
        if pos.side == "long":
            return (price - pos.entry_price) * pos.amount
        return (pos.entry_price - price) * pos.amount

    def roe_pct(self, prices: dict[str, float]) -> float | None:
        pos = self.position
        if pos is None or pos.margin <= 0:
            return None
        return (self.unrealized_pnl(prices) / pos.margin) * 100

    def mark_to_market(self, prices: dict[str, float]) -> float:
        total = self.capital + self.unrealized_pnl(prices)
        if self.position:
            total += self.position.margin
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

        closes = [t for t in self.trades if t["action"] == "close"]
        wins = [t for t in closes if t.get("pnl", 0) > 0]
        losses = [t for t in closes if t.get("pnl", 0) <= 0]
        gross_profit = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losses))

        sharpe = 0.0
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(365 * 24))

        days = max(1, len(self.equity_curve) / 24)
        return {
            "initial_capital": self.initial_capital,
            "final_value": float(equity[-1]),
            "total_return_pct": float((equity[-1] / self.initial_capital - 1) * 100),
            "max_drawdown_pct": float(max_dd * 100),
            "sharpe_ratio": round(sharpe, 3),
            "total_trades": len(closes),
            "trades_per_day": round(len(closes) / days, 2),
            "win_rate": len(wins) / len(closes) * 100 if closes else 0,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        }


async def run_backtest(symbols: list[str], days: int = 90) -> BacktestResult:
    client = ExchangeClient()
    quant = QuantEngine(profile="extreme")
    signal_engine = SignalEngine(profile="extreme")
    risk_cfg = load_risk_config(profile="extreme")
    exit_cfg = risk_cfg.get("exit_rules", {})
    tp_roe = float(exit_cfg.get("take_profit_roe_pct", 12.0))
    sl_roe = float(exit_cfg.get("stop_loss_roe_pct", -8.0))
    leverage = int(risk_cfg.get("derivatives", {}).get("max_leverage", 20))
    fee_rate = float(risk_cfg.get("fees", {}).get("perp_taker_rate", 0.0005))

    result = BacktestResult()
    ohlcv_by_symbol: dict[str, list[list]] = {}

    for symbol in symbols:
        logger.info("Loading %s...", symbol)
        ohlcv = await client.fetch_ohlcv(symbol, timeframe="1h", limit=min(days * 24, 1000))
        if len(ohlcv) < 60:
            logger.warning("Insufficient data for %s", symbol)
            continue
        ohlcv_by_symbol[symbol] = ohlcv

    if not ohlcv_by_symbol:
        return result

    min_len = min(len(v) for v in ohlcv_by_symbol.values())
    warmup = 55

    for i in range(warmup, min_len):
        prices: dict[str, float] = {}
        mtf_data: dict[str, dict[str, list[list]]] = {}
        for symbol, ohlcv in ohlcv_by_symbol.items():
            window = ohlcv[: i + 1]
            prices[symbol] = window[-1][4]
            mtf_data[symbol] = {"1h": window, "4h": window, "1d": window}

        signals = quant.analyze_all_multi(mtf_data)
        deriv_by_symbol = {
            s.symbol: DerivativesSnapshot(
                symbol=s.symbol,
                funding_rate=0.0,
                funding_rate_pct=0.0,
                mark_price=prices[s.symbol],
                open_interest=1_000_000.0,
                timestamp=app_now(),
            )
            for s in signals
        }
        snapshot = PortfolioSnapshot(
            mode=TradingMode.PAPER,
            profile="extreme",
            quote_balance=result.capital,
            total_value_usdt=result.mark_to_market(prices),
            unrealized_pnl=result.unrealized_pnl(prices),
            realized_pnl=0.0,
            positions=[],
        )

        if result.position:
            roe = result.roe_pct(prices)
            if roe is not None:
                if roe >= tp_roe:
                    result.close_position(
                        prices[result.position.symbol],
                        fee_rate,
                        ohlcv_by_symbol[result.position.symbol][i][0],
                        "roe_tp",
                    )
                elif roe <= sl_roe:
                    result.close_position(
                        prices[result.position.symbol],
                        fee_rate,
                        ohlcv_by_symbol[result.position.symbol][i][0],
                        "roe_sl",
                    )
        else:
            evaluation = signal_engine.evaluate(signals, deriv_by_symbol, snapshot)
            if evaluation.decision.proposals:
                proposal = evaluation.decision.proposals[0]
                price = prices.get(proposal.symbol, 0)
                if price > 0:
                    result.open_position(
                        proposal.symbol,
                        proposal.position_side,
                        price,
                        leverage,
                        fee_rate,
                        ohlcv_by_symbol[proposal.symbol][i][0],
                    )

        result.mark_to_market(prices)

    if result.position:
        sym = result.position.symbol
        result.close_position(prices[sym], fee_rate, ohlcv_by_symbol[sym][-1][0], "end")

    return result


async def main():
    parser = argparse.ArgumentParser(description="AInvestor backtest (extreme perps)")
    parser.add_argument(
        "--symbols",
        default="BTC/USDT,ETH/USDT,SOL/USDT",
        help="Comma-separated pairs",
    )
    parser.add_argument("--days", type=int, default=90, help="Days of history")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    result = await run_backtest(symbols, args.days)
    metrics = result.metrics()

    print("\n=== Backtest Results (extreme 20x all-in) ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    btc_client = ExchangeClient()
    btc_ohlcv = await btc_client.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=min(args.days * 24, 1000))
    if btc_ohlcv:
        btc_return = (btc_ohlcv[-1][4] / btc_ohlcv[0][4] - 1) * 100
        print(f"\n  BTC buy-and-hold return: {btc_return:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
