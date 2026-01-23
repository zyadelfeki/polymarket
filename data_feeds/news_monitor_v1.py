import asyncio
import aiohttp
from datetime import datetime
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class NewsSeverity(Enum):
    """News impact classification."""
    CRITICAL = 3
    HIGH = 2
    MEDIUM = 1
    LOW = 0


@dataclass
class NewsAlert:
    """Detected news item with market impact prediction."""
    timestamp: datetime
    source: str
    headline: str
    keywords: List[str]
    severity: NewsSeverity
    affected_markets: List[str]
    predicted_direction: str
    confidence: float
    raw_text: str


class NewsMonitor:
    """
    Real-time news monitoring for Polymarket arbitrage.

    Monitors:
    - Twitter feeds (@Reuters, @AP, @SEC_News, @elonmusk)
    - News APIs (NewsAPI, Cryptopanic)
    - Crypto social sentiment (santiment.net data if available)
    """

    CRITICAL_SOURCES = [
        "@Reuters",
        "@AP",
        "@SEC_News",
        "@elonmusk",
        "@federalreserve",
        "@POTUS"
    ]

    TRIGGER_KEYWORDS = {
        "SEC": "crypto",
        "regulations": "crypto",
        "Bitcoin ETF": "BTC",
        "Ethereum": "ETH",
        "cryptocurrency": "crypto",
        "blockchain": "crypto",
        "Trump": "trump",
        "Biden": "biden",
        "congress": "politics",
        "senate": "politics",
        "election": "politics",
        "indictment": "trump",
        "investigation": "politics",
        "Fed": "macro",
        "inflation": "macro",
        "interest rates": "macro",
        "unemployment": "macro",
        "recession": "macro",
        "unemployment rate": "macro",
        "CPI": "macro",
        "GDP": "macro",
        "stock market": "markets",
        "nasdaq": "markets",
        "S&P 500": "markets",
        "Dow Jones": "markets",
        "earnings": "earnings",
        "earnings beat": "earnings",
        "revenue": "earnings",
    }

    BULLISH_KEYWORDS = ["surges", "gains", "jumps", "soars", "bulls", "beat", "strong", "positive", "approved", "bullish"]
    BEARISH_KEYWORDS = ["crashes", "plunges", "falls", "drops", "bears", "miss", "weak", "negative", "rejected", "bearish"]

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.news_history: List[NewsAlert] = []
        self.market_alerts: Dict[str, List[NewsAlert]] = {}
        self.callbacks: List[Callable] = []
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None

        self.twitter_rate_limit = RateLimiter(calls=30, period=900)
        self.newsapi_rate_limit = RateLimiter(calls=100, period=86400)

    async def initialize(self):
        """Start monitoring."""
        self.session = aiohttp.ClientSession()
        self.running = True
        logger.info("✅ News monitor initialized")

    async def register_callback(self, callback: Callable):
        """Register callback for news alerts."""
        self.callbacks.append(callback)

    async def monitor_twitter(self) -> List[NewsAlert]:
        """Monitor critical Twitter sources via nitter RSS."""
        alerts: List[NewsAlert] = []

        try:
            for source in self.CRITICAL_SOURCES:
                username = source.replace("@", "")
                url = f"https://nitter.net/{username}/rss"

                try:
                    async with self.session.get(url, timeout=5) as resp:
                        if resp.status != 200:
                            continue

                        text = await resp.text()
                        tweets = self._parse_rss_tweets(text)

                        for tweet_text, tweet_time in tweets[:5]:
                            alert = await self._analyze_tweet(tweet_text, tweet_time, username)
                            if alert:
                                alerts.append(alert)

                except Exception as e:
                    logger.warning(f"Twitter fetch error for {source}: {e}")

        except Exception as e:
            logger.error(f"Twitter monitoring error: {e}")

        return alerts

    async def monitor_news_api(self) -> List[NewsAlert]:
        """Monitor traditional news via NewsAPI."""
        alerts: List[NewsAlert] = []

        try:
            keywords = ["cryptocurrency", "SEC", "Bitcoin", "Trump", "inflation", "Fed"]

            for keyword in keywords:
                url = "https://newsapi.org/v2/everything"
                params = {
                    "q": keyword,
                    "sortBy": "publishedAt",
                    "language": "en",
                    "apiKey": self.config.get("NEWS_API_KEY", "demo"),
                }

                try:
                    async with self.session.get(url, params=params, timeout=10) as resp:
                        if resp.status != 200:
                            continue

                        data = await resp.json()
                        articles = data.get("articles", [])

                        for article in articles[:3]:
                            alert = await self._analyze_article(article, keyword)
                            if alert:
                                alerts.append(alert)

                except Exception as e:
                    logger.warning(f"NewsAPI error for '{keyword}': {e}")

        except Exception as e:
            logger.error(f"News API monitoring error: {e}")

        return alerts

    async def _analyze_tweet(self, text: str, timestamp: datetime, source: str) -> Optional[NewsAlert]:
        keywords = []
        for keyword, category in self.TRIGGER_KEYWORDS.items():
            if keyword.lower() in text.lower():
                keywords.append(keyword)

        if not keywords:
            return None

        direction = "UNCLEAR"
        bullish_count = sum(1 for kw in self.BULLISH_KEYWORDS if kw in text.lower())
        bearish_count = sum(1 for kw in self.BEARISH_KEYWORDS if kw in text.lower())

        if bullish_count > bearish_count:
            direction = "UP"
        elif bearish_count > bullish_count:
            direction = "DOWN"

        severity = NewsSeverity.LOW
        if any(crit in source for crit in ["Reuters", "AP", "SEC"]):
            severity = NewsSeverity.CRITICAL
        elif bullish_count + bearish_count > 2:
            severity = NewsSeverity.HIGH

        confidence = min(0.95, 0.6 + (bullish_count + bearish_count) * 0.1)

        affected = []
        for keyword, category in self.TRIGGER_KEYWORDS.items():
            if keyword.lower() in text.lower():
                affected.append(category.upper())

        alert = NewsAlert(
            timestamp=timestamp,
            source="twitter",
            headline=text[:100],
            keywords=keywords,
            severity=severity,
            affected_markets=list(set(affected)),
            predicted_direction=direction,
            confidence=confidence,
            raw_text=text
        )

        logger.info(f"🔔 News alert: {alert.headline} | Direction: {direction} | Confidence: {confidence:.0%}")
        return alert

    async def _analyze_article(self, article: Dict, source_keyword: str) -> Optional[NewsAlert]:
        title = article.get("title", "")
        description = article.get("description", "")
        text = f"{title} {description}"

        keywords = []
        for keyword, category in self.TRIGGER_KEYWORDS.items():
            if keyword.lower() in text.lower():
                keywords.append(keyword)

        if not keywords:
            return None

        direction = "UNCLEAR"
        bullish_count = sum(1 for kw in self.BULLISH_KEYWORDS if kw in text.lower())
        bearish_count = sum(1 for kw in self.BEARISH_KEYWORDS if kw in text.lower())

        if bullish_count > bearish_count:
            direction = "UP"
        elif bearish_count > bullish_count:
            direction = "DOWN"

        source_name = article.get("source", {}).get("name", "").lower()
        if any(rep in source_name for rep in ["reuters", "ap", "bloomberg", "cnbc"]):
            severity = NewsSeverity.CRITICAL
        else:
            severity = NewsSeverity.MEDIUM

        confidence = min(0.95, 0.5 + (bullish_count + bearish_count) * 0.15)

        affected = list(set([self.TRIGGER_KEYWORDS.get(kw, "other").upper() for kw in keywords]))

        alert = NewsAlert(
            timestamp=datetime.fromisoformat(article.get("publishedAt", "").replace("Z", "+00:00")),
            source="newsapi",
            headline=title,
            keywords=keywords,
            severity=severity,
            affected_markets=affected,
            predicted_direction=direction,
            confidence=confidence,
            raw_text=text
        )

        logger.info(f"📰 Article: {alert.headline} | Direction: {direction} | Confidence: {confidence:.0%}")
        return alert

    def _parse_rss_tweets(self, rss_xml: str) -> List[tuple]:
        tweets = []

        import re

        items = re.findall(r"<item>(.*?)</item>", rss_xml, re.DOTALL)

        for item in items:
            title_match = re.search(r"<title>(.*?)</title>", item)
            date_match = re.search(r"<pubDate>(.*?)</pubDate>", item)

            if title_match:
                text = title_match.group(1)
                date_str = date_match.group(1) if date_match else None
                tweets.append((text, date_str))

        return tweets

    async def get_recent_alerts(self, market: str = None, limit: int = 10) -> List[NewsAlert]:
        alerts = self.news_history[-limit:]

        if market:
            alerts = [a for a in alerts if market in a.affected_markets]

        return alerts

    async def run_monitoring_loop(self):
        while self.running:
            try:
                twitter_alerts = await self.monitor_twitter()
                news_alerts = await self.monitor_news_api()

                all_alerts = twitter_alerts + news_alerts

                for alert in all_alerts:
                    if not any(a.headline == alert.headline for a in self.news_history[-20:]):
                        self.news_history.append(alert)

                        for callback in self.callbacks:
                            await callback(alert)

                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                await asyncio.sleep(10)

    async def close(self):
        self.running = False
        if self.session:
            await self.session.close()


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, calls: int, period: int):
        self.calls = calls
        self.period = period
        self.tokens = calls
        self.last_update = datetime.utcnow()

    async def acquire(self):
        """Wait if necessary."""
        while True:
            now = datetime.utcnow()
            elapsed = (now - self.last_update).total_seconds()
            self.tokens = min(self.calls, self.tokens + elapsed * (self.calls / self.period))
            self.last_update = now

            if self.tokens >= 1:
                self.tokens -= 1
                return

            await asyncio.sleep(0.1)
