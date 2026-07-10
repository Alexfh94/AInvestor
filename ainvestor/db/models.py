from __future__ import annotations

from datetime import datetime

from ainvestor.utils.datetime_utils import app_now
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=app_now, onupdate=app_now
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
    instrument_type: Mapped[str] = mapped_column(String(20), default="spot")
    position_side: Mapped[str] = mapped_column(String(10), default="long")
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    margin_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    asset_class: Mapped[str] = mapped_column(String(20), default="crypto")
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)
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
    instrument_type: Mapped[str] = mapped_column(String(20), default="spot")
    position_side: Mapped[str] = mapped_column(String(10), default="long")
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    asset_class: Mapped[str] = mapped_column(String(20), default="crypto")
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)
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
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=app_now, index=True)


class PortfolioValueHistory(Base):
    __tablename__ = "portfolio_value_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    total_value_usdt: Mapped[float] = mapped_column(Float)
    quote_balance: Mapped[float] = mapped_column(Float)
    invested_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=app_now, index=True)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50))
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)


class CycleRun(Base):
    __tablename__ = "cycle_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NewsRecord(Base):
    __tablename__ = "news_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    currencies: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=app_now, index=True)


class SentimentRecord(Base):
    __tablename__ = "sentiment_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fear_greed_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fear_greed_label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    btc_dominance: Mapped[float | None] = mapped_column(Float, nullable=True)
    reddit_mentions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=app_now, index=True)


class DerivativesRecord(Base):
    __tablename__ = "derivatives_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    funding_rate: Mapped[float] = mapped_column(Float)
    funding_rate_pct: Mapped[float] = mapped_column(Float)
    mark_price: Mapped[float] = mapped_column(Float)
    open_interest: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=app_now, index=True)


class StockPortfolio(Base):
    __tablename__ = "stock_portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(20), default="paper")
    cash_eur: Mapped[float] = mapped_column(Float, default=0.0)
    cash_usd: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_eur: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=app_now, onupdate=app_now
    )


class StockPosition(Base):
    __tablename__ = "stock_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    shares: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=app_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)


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


def _migrate_utc_timestamps_to_madrid() -> None:
    """Convierte timestamps históricos guardados en UTC naive a Europe/Madrid naive."""
    from sqlalchemy import inspect, text

    from ainvestor.utils.datetime_utils import utc_naive_to_madrid_naive

    migration_key = "timestamps_utc_to_madrid_v1"
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS app_meta "
                "(key TEXT PRIMARY KEY, value TEXT)"
            )
        )
        done = conn.execute(
            text("SELECT value FROM app_meta WHERE key = :k"),
            {"k": migration_key},
        ).fetchone()
        if done:
            return

    datetime_columns: dict[str, list[str]] = {
        "portfolios": ["created_at", "updated_at"],
        "positions": ["opened_at", "closed_at"],
        "trades": ["executed_at"],
        "ai_decisions": ["created_at"],
        "decision_outcomes": ["created_at", "evaluated_at"],
        "market_snapshots": ["captured_at"],
        "portfolio_value_history": ["captured_at"],
        "risk_events": ["created_at"],
        "cycle_runs": ["started_at", "completed_at"],
        "news_records": ["published_at", "captured_at"],
        "sentiment_records": ["captured_at"],
        "derivatives_records": ["captured_at"],
        "stock_portfolios": ["created_at", "updated_at"],
        "stock_positions": ["opened_at", "closed_at"],
    }

    inspector = inspect(engine)
    db = SessionLocal()
    try:
        from sqlalchemy import update

        for table, columns in datetime_columns.items():
            if table not in inspector.get_table_names():
                continue
            table_cols = {c["name"] for c in inspector.get_columns(table)}
            model = Base.metadata.tables[table]
            pk_cols = [c.name for c in model.primary_key.columns]
            for col in columns:
                if col not in table_cols:
                    continue
                rows = db.execute(model.select()).fetchall()
                for row in rows:
                    value = row._mapping[col]
                    if value is None:
                        continue
                    converted = utc_naive_to_madrid_naive(value)
                    if converted == value:
                        continue
                    where = [model.c[name] == row._mapping[name] for name in pk_cols]
                    db.execute(update(model).where(*where).values({col: converted}))
        db.commit()
    finally:
        db.close()

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO app_meta (key, value) VALUES (:k, :v)"),
            {"k": migration_key, "v": "done"},
        )


def _migrate_db() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    _migrate_utc_timestamps_to_madrid()

    if "ai_decisions" in table_names:
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

    for table, new_cols in (
        ("positions", {
            "instrument_type": "VARCHAR(20) DEFAULT 'spot'",
            "position_side": "VARCHAR(10) DEFAULT 'long'",
            "leverage": "INTEGER DEFAULT 1",
            "margin_used": "FLOAT",
            "asset_class": "VARCHAR(20) DEFAULT 'crypto'",
        }),
        ("trades", {
            "instrument_type": "VARCHAR(20) DEFAULT 'spot'",
            "position_side": "VARCHAR(10) DEFAULT 'long'",
            "leverage": "INTEGER DEFAULT 1",
            "asset_class": "VARCHAR(20) DEFAULT 'crypto'",
        }),
        ("decision_outcomes", {
            "instrument_type": "VARCHAR(20) DEFAULT 'spot'",
        }),
    ):
        if table not in table_names:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for col, col_type in new_cols.items():
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))

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
