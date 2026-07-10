"""Hora de aplicación en Europe/Madrid (España peninsular)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

APP_TZ = ZoneInfo("Europe/Madrid")


def app_now() -> datetime:
    """Datetime naive en hora de España para SQLite y registros."""
    return datetime.now(APP_TZ).replace(tzinfo=None)


def app_now_iso() -> str:
    return format_app_datetime(app_now())


def assume_madrid(dt: datetime) -> datetime:
    """Interpreta un datetime naive de BD como hora de Madrid."""
    return dt.replace(tzinfo=APP_TZ)


def utc_naive_to_madrid_naive(dt: datetime) -> datetime:
    """Convierte un datetime naive UTC (histórico) a naive Madrid."""
    return (
        dt.replace(tzinfo=timezone.utc)
        .astimezone(APP_TZ)
        .replace(tzinfo=None)
    )


def format_app_datetime(dt: datetime | None) -> str | None:
    """Serializa un datetime naive de BD con offset Europe/Madrid para la API."""
    if dt is None:
        return None
    return assume_madrid(dt).isoformat(timespec="seconds")
