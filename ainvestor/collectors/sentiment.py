from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from ainvestor.utils.datetime_utils import app_now

import httpx
from sqlalchemy.orm import Session

from ainvestor.config import get_all_market_pairs, get_settings
from ainvestor.db.models import SentimentRecord
from ainvestor.models.schemas import SentimentData

logger = logging.getLogger(__name__)

FEAR_GREED_URL = "https://api.alternative.me/fng/"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_HOT_URL = "https://oauth.reddit.com/r/cryptocurrency/hot"


class SentimentCollector:
    """Collects fear/greed index, Reddit mentions and persists to DB."""

    def __init__(self, db: Session | None = None):
        self.settings = get_settings()
        self._pairs = get_all_market_pairs()
        self.db = db

    async def collect(self, btc_dominance: float | None = None, persist: bool = True) -> SentimentData:
        fear_greed = await self._fetch_fear_greed()
        reddit_mentions = await self._fetch_reddit_mentions()

        data = SentimentData(
            fear_greed_index=fear_greed.get("value"),
            fear_greed_label=fear_greed.get("classification"),
            reddit_mentions=reddit_mentions,
            timestamp=app_now(),
        )

        if persist and self.db is not None:
            record = SentimentRecord(
                fear_greed_index=data.fear_greed_index,
                fear_greed_label=data.fear_greed_label,
                btc_dominance=btc_dominance,
                reddit_mentions_json=json.dumps(reddit_mentions) if reddit_mentions else None,
            )
            self.db.add(record)
            self.db.commit()

        return data

    async def _fetch_fear_greed(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(FEAR_GREED_URL, params={"limit": 1})
                resp.raise_for_status()
                data = resp.json()
                if data.get("data"):
                    item = data["data"][0]
                    return {
                        "value": int(item.get("value", 50)),
                        "classification": item.get("value_classification", "Neutral"),
                    }
        except Exception as e:
            logger.warning("Fear & Greed fetch failed: %s", e)
        return {"value": None, "classification": None}

    async def _fetch_reddit_mentions(self) -> dict[str, int]:
        if not self.settings.reddit_client_id:
            return {}

        try:
            token = await self._get_reddit_token()
            if not token:
                return {}

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    REDDIT_HOT_URL,
                    params={"limit": 50},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": self.settings.reddit_user_agent,
                    },
                )
                resp.raise_for_status()
                posts = resp.json().get("data", {}).get("children", [])

            symbols = {p.replace("/USDT", "").upper() for p in self._pairs}
            mentions: dict[str, int] = {s: 0 for s in symbols}

            for post in posts:
                text = (
                    post.get("data", {}).get("title", "")
                    + " "
                    + post.get("data", {}).get("selftext", "")
                ).upper()
                for sym in symbols:
                    if re.search(rf"\b{sym}\b", text):
                        mentions[sym] += 1

            return {k: v for k, v in mentions.items() if v > 0}
        except Exception as e:
            logger.warning("Reddit fetch failed: %s", e)
            return {}

    async def _get_reddit_token(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    REDDIT_TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(
                        self.settings.reddit_client_id,
                        self.settings.reddit_client_secret,
                    ),
                    headers={"User-Agent": self.settings.reddit_user_agent},
                )
                resp.raise_for_status()
                return resp.json().get("access_token")
        except Exception as e:
            logger.warning("Reddit auth failed: %s", e)
            return None

    def get_latest_from_db(self) -> SentimentRecord | None:
        if self.db is None:
            return None
        return (
            self.db.query(SentimentRecord)
            .order_by(SentimentRecord.captured_at.desc())
            .first()
        )

    def summarize(self, data: SentimentData, btc_dominance: float | None = None) -> str:
        lines = []
        if data.fear_greed_index is not None:
            lines.append(
                f"Fear & Greed Index: {data.fear_greed_index} ({data.fear_greed_label})"
            )
        if btc_dominance is not None:
            lines.append(f"BTC dominance: {btc_dominance:.1f}%")
        if data.reddit_mentions:
            top = sorted(data.reddit_mentions.items(), key=lambda x: -x[1])[:5]
            lines.append("Reddit hot mentions: " + ", ".join(f"{k}({v})" for k, v in top))
        return "\n".join(lines) if lines else "No sentiment data available."
