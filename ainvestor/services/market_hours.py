from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
US_MARKET_OPEN = time(15, 30)
US_MARKET_CLOSE = time(22, 0)


def is_us_market_open(now: datetime | None = None) -> bool:
    """NYSE regular session approx. in Europe/Madrid (CET/CEST)."""
    now = now or datetime.now(MADRID)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return US_MARKET_OPEN <= t <= US_MARKET_CLOSE


def market_status_label() -> str:
    if is_us_market_open():
        return "US equities: OPEN"
    return "US equities: CLOSED"
