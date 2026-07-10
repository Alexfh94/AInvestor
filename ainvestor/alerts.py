from __future__ import annotations

import logging

import httpx

from ainvestor.config import get_settings

logger = logging.getLogger(__name__)


async def send_telegram_alert(message: str) -> bool:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                json={"chat_id": settings.telegram_chat_id, "text": message},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning("Telegram alert failed: %s", e)
        return False
