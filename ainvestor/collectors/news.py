from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ainvestor.config import get_settings
from ainvestor.models.schemas import NewsItem

logger = logging.getLogger(__name__)

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"


class NewsCollector:
    """Collects crypto news from CryptoPanic API."""

    def __init__(self):
        self.api_key = get_settings().cryptopanic_api_key

    async def collect(self, currencies: list[str] | None = None) -> list[NewsItem]:
        if not self.api_key:
            return await self._collect_rss_fallback()

        params: dict = {
            "auth_token": self.api_key,
            "public": "true",
            "kind": "news",
        }
        if currencies:
            params["currencies"] = ",".join(c.replace("/USDT", "") for c in currencies)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(CRYPTOPANIC_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("CryptoPanic failed: %s", e)
            return await self._collect_rss_fallback()

        items: list[NewsItem] = []
        for post in data.get("results", [])[:20]:
            votes = post.get("votes", {})
            sentiment = None
            if votes.get("positive", 0) > votes.get("negative", 0):
                sentiment = "positive"
            elif votes.get("negative", 0) > votes.get("positive", 0):
                sentiment = "negative"
            else:
                sentiment = "neutral"

            currencies_list = [
                c.get("code", "") for c in post.get("currencies", [])
            ]
            published = None
            if post.get("published_at"):
                try:
                    published = datetime.fromisoformat(
                        post["published_at"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            items.append(
                NewsItem(
                    title=post.get("title", ""),
                    url=post.get("url", ""),
                    source=post.get("source", {}).get("title", "CryptoPanic"),
                    published_at=published,
                    currencies=currencies_list,
                    sentiment=sentiment,
                )
            )
        return items

    async def _collect_rss_fallback(self) -> list[NewsItem]:
        """Fallback when no API key - return empty with log."""
        logger.info("No CRYPTOPANIC_API_KEY - news collection skipped")
        return []

    def summarize(self, items: list[NewsItem], max_items: int = 10) -> str:
        if not items:
            return "No recent news available."
        lines = []
        for item in items[:max_items]:
            sent = f" [{item.sentiment}]" if item.sentiment else ""
            curr = f" ({', '.join(item.currencies)})" if item.currencies else ""
            lines.append(f"- {item.title}{sent}{curr}")
        return "\n".join(lines)
