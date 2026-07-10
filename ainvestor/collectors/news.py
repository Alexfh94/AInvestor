from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ainvestor.config import get_settings
from ainvestor.db.models import NewsRecord
from ainvestor.models.schemas import NewsItem

logger = logging.getLogger(__name__)

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
]


class NewsCollector:
    """Collects crypto news from CryptoPanic API with RSS fallback and DB persistence."""

    def __init__(self, db: Session | None = None):
        self.api_key = get_settings().cryptopanic_api_key
        self.db = db

    async def collect(
        self, currencies: list[str] | None = None, persist: bool = True
    ) -> list[NewsItem]:
        items: list[NewsItem] = []
        if self.api_key:
            items = await self._collect_cryptopanic(currencies)
        if not items:
            items = await self._collect_rss_fallback()

        if currencies:
            items = self.filter_by_pairs(items, currencies)

        if persist and self.db is not None:
            self._persist(items)

        return items

    def filter_by_pairs(self, items: list[NewsItem], pairs: list[str]) -> list[NewsItem]:
        bases = {p.split("/")[0].upper() for p in pairs}
        bases.add("BTC")
        filtered = []
        for item in items:
            item_currencies = {c.upper() for c in item.currencies}
            title_upper = item.title.upper()
            if item_currencies & bases or any(b in title_upper for b in bases):
                filtered.append(item)
        return filtered if filtered else items[:10]

    async def _collect_cryptopanic(self, currencies: list[str] | None) -> list[NewsItem]:
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
            return []

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

            currencies_list = [c.get("code", "") for c in post.get("currencies", [])]
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
        items: list[NewsItem] = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for source_name, feed_url in RSS_FEEDS:
                try:
                    resp = await client.get(feed_url)
                    resp.raise_for_status()
                    items.extend(self._parse_rss(resp.text, source_name))
                except Exception as e:
                    logger.warning("RSS fetch failed %s: %s", source_name, e)
        return items[:25]

    def _parse_rss(self, xml_text: str, source: str) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            root = ET.fromstring(xml_text)
            for item in root.findall(".//item")[:10]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pub = item.findtext("pubDate")
                published = None
                if pub:
                    try:
                        from email.utils import parsedate_to_datetime

                        published = parsedate_to_datetime(pub)
                    except Exception:
                        pass
                items.append(
                    NewsItem(
                        title=title,
                        url=link,
                        source=source,
                        published_at=published,
                        currencies=[],
                        sentiment="neutral",
                    )
                )
        except ET.ParseError as e:
            logger.warning("RSS parse error %s: %s", source, e)
        return items

    def _persist(self, items: list[NewsItem]) -> None:
        if self.db is None:
            return
        for item in items[:20]:
            record = NewsRecord(
                title=item.title[:500],
                url=item.url,
                source=item.source,
                currencies=",".join(item.currencies) if item.currencies else None,
                sentiment=item.sentiment,
                published_at=item.published_at,
            )
            self.db.add(record)
        self.db.commit()

    def get_recent_from_db(self, limit: int = 20) -> list[NewsRecord]:
        if self.db is None:
            return []
        return (
            self.db.query(NewsRecord)
            .order_by(NewsRecord.captured_at.desc())
            .limit(limit)
            .all()
        )

    def summarize(self, items: list[NewsItem], max_items: int = 10) -> str:
        if not items:
            return "No recent news available."
        lines = []
        for item in items[:max_items]:
            sent = f" [{item.sentiment}]" if item.sentiment else ""
            curr = f" ({', '.join(item.currencies)})" if item.currencies else ""
            lines.append(f"- {item.title}{sent}{curr}")
        return "\n".join(lines)
