from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from ainvestor.config import get_settings


class Base(DeclarativeBase):
    pass


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(20), default="paper")
    quote_balance: Mapped[float] = mapped_column(Float, default=100.0)
    quote_currency: Mapped[str] = mapped_column(String(10), default="USDT")
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    amount: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(10))
    amount: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    value_usdt: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="executed")
    mode: Mapped[str] = mapped_column(String(20), default="paper")
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIDecision(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    model: Mapped[str] = mapped_column(String(50))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    hold: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    prompt_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposals_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tokens_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_cache_read: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_cache_write: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DecisionOutcome(Base):
    __tablename__ = "decision_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    record_type: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(10))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    conviction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_status: Mapped[str] = mapped_column(String(20), default="hold")
    price_at_decision: Mapped[float] = mapped_column(Float, default=0.0)
    price_at_evaluation: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    outcome_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    last_price: Mapped[float] = mapped_column(Float)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    ohlcv_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PortfolioValueHistory(Base):
    __tablename__ = "portfolio_value_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    total_value_usdt: Mapped[float] = mapped_column(Float)
    quote_balance: Mapped[float] = mapped_column(Float)
    invested_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50))
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CycleRun(Base):
    __tablename__ = "cycle_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in _settings.database_url else {},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    get_settings().data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    _sync_paper_balance()


def _migrate_db() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "ai_decisions" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("ai_decisions")}
        with engine.begin() as conn:
            if "summary" not in cols:
                conn.execute(text("ALTER TABLE ai_decisions ADD COLUMN summary TEXT"))
            if "hold" not in cols:
                conn.execute(text("ALTER TABLE ai_decisions ADD COLUMN hold BOOLEAN"))
            for col in (
                "tokens_input",
                "tokens_output",
                "tokens_cache_read",
                "tokens_cache_write",
                "tokens_total",
            ):
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE ai_decisions ADD COLUMN {col} INTEGER"))
    _backfill_decision_summaries()


def _backfill_decision_summaries() -> None:
    from ainvestor.engine.ai_agent import parse_trade_proposal

    db = SessionLocal()
    try:
        decisions = db.query(AIDecision).filter(AIDecision.summary.is_(None)).all()
        for d in decisions:
            if not d.raw_response:
                continue
            try:
                parsed = parse_trade_proposal(d.raw_response)
                d.summary = parsed.summary
                d.hold = parsed.hold
            except Exception:
                continue
        if decisions:
            db.commit()
    finally:
        db.close()


def _sync_paper_balance() -> None:
    """Ajusta cartera paper al saldo configurado si no hay operaciones."""
    settings = get_settings()
    db = SessionLocal()
    try:
        portfolio = (
            db.query(Portfolio)
            .filter(Portfolio.mode == settings.trading_mode)
            .first()
        )
        if portfolio is None:
            return
        has_trades = (
            db.query(Trade).filter(Trade.portfolio_id == portfolio.id).count() > 0
        )
        has_positions = (
            db.query(Position)
            .filter(Position.portfolio_id == portfolio.id, Position.is_open == True)  # noqa: E712
            .count()
            > 0
        )
        if not has_trades and not has_positions:
            portfolio.quote_balance = settings.paper_initial_balance
            portfolio.realized_pnl = 0.0
            db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
