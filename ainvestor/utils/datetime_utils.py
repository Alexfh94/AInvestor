"""Hora de aplicación en Europe/Madrid (España peninsular)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

APP_TZ = ZoneInfo("Europe/Madrid")


def app_now() -> datetime:
    """Datetime naive en hora de España para SQLite y registros."""
    return datetime.now(APP_TZ).replace(tzinfo=None)


def app_now_iso() -> str:
    return datetime.now(APP_TZ).isoformat(timespec="seconds")
