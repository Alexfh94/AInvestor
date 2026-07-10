from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class TradingMode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TradeStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DecisionAction(str, Enum):
    HOLD = "hold"
    BUY = "buy"
    SELL = "sell"


class TradeProposal(BaseModel):
    """AI proposal - must pass RiskManager before execution."""

    action: DecisionAction
    symbol: str
    amount_pct: float = Field(ge=0, le=100, description="% of available quote balance")
    stop_loss_pct: float = Field(ge=0, le=100)
    take_profit_pct: float = Field(ge=0, le=100)
    conviction: int = Field(ge=0, le=100, default=50)
    reasoning: str = ""


class CycleDecision(BaseModel):
    """Full AI cycle output."""

    proposals: list[TradeProposal] = Field(default_factory=list)
    summary: str = ""
    hold: bool = False


class AIUsage(BaseModel):
    """Token usage for an AI cycle (comprobante de consumo)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
        }


class PositionSnapshot(BaseModel):
    symbol: str
    asset: str
    amount: float
    entry_price: float
    current_price: float
    value_usdt: float
    pct_of_portfolio: float
    unrealized_pnl: float
    stop_loss: float | None = None
    take_profit: float | None = None


class PortfolioSnapshot(BaseModel):
    mode: TradingMode
    quote_balance: float
    total_value_usdt: float
    invested_usdt: float = 0.0
    cash_pct: float = 100.0
    unrealized_pnl: float
    realized_pnl: float
    positions: list[PositionSnapshot] = Field(default_factory=list)
    kill_switch_active: bool = False


class TechnicalSignal(BaseModel):
    symbol: str
    rsi: float | None = None
    ma_fast: float | None = None
    ma_slow: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    volume_ratio: float | None = None
    conviction_score: int = Field(ge=0, le=100, default=50)
    trend: Literal["bullish", "bearish", "neutral"] = "neutral"


class RiskCheckResult(BaseModel):
    approved: bool
    proposal: TradeProposal | None = None
    rejection_reasons: list[str] = Field(default_factory=list)


class MarketTicker(BaseModel):
    symbol: str
    last: float
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    change_pct: float | None = None
    timestamp: datetime


class NewsItem(BaseModel):
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    currencies: list[str] = Field(default_factory=list)
    sentiment: Literal["positive", "negative", "neutral"] | None = None


class SentimentData(BaseModel):
    fear_greed_index: int | None = None
    fear_greed_label: str | None = None
    reddit_mentions: dict[str, int] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
