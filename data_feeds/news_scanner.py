import aiohttp
import asyncio
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

class NewsItem:
    def __init__(self, title: str, description: str, source: str, published_at: datetime, url: str, relevance_score: float = 0.0):
        self.title = title
        self.description = description
        self.source = source
        self.published_at = published_at
        self.url = url
        self.relevance_score = relevance_score
    
    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "url": self.url,
            "relevance_score": self.relevance_score
        }
    
    def __repr__(self):
        return f"NewsItem(title='{self.title[:50]}...', source={self.source})"

class NewsScanner:
    def __init__(self):
        self.newsapi_key = settings.NEWS_API_KEY
        self.last_fetch = {}
        self.cache = []
        self.high_priority_keywords = [
            "bitcoin", "btc", "ethereum", "eth", "federal reserve", "fed",
            "rate decision", "sec", "regulation", "hack", "exploit",
            "whale", "elon musk", "trump", "binance", "coinbase"
        ]
    
    async def fetch_newsapi(self, session: aiohttp.ClientSession, lookback_minutes: int = 10) -> List[NewsItem]:
        if not self.newsapi_key:
            logger.warning("NewsAPI key not configured")
            return []
        
        url = "https://newsapi.org/v2/everything"
        from_time = datetime.utcnow() - timedelta(minutes=lookback_minutes)
        params = {
            "apiKey": self.newsapi_key,
            "q": "bitcoin OR ethereum OR cryptocurrency OR federal reserve",
            "language": "en",
            "sortBy": "publishedAt",
            "from": from_time.isoformat(),
            "pageSize": 20
        }
        
        try:
            async with session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    articles = data.get("articles", [])
                    news_items = []
                    for article in articles:
                        try:
                            news_item = NewsItem(
                                title=article.get("title", ""),
                                description=article.get("description", ""),
                                source=article.get("source", {}).get("name", "NewsAPI"),
                                published_at=datetime.fromisoformat(article["publishedAt"].replace("Z", "+00:00")),
                                url=article.get("url", ""),
                                relevance_score=self._calculate_relevance(article)
                            )
                            news_items.append(news_item)
                        except:
                            continue
                    logger.info(f"✅ NewsAPI: {len(news_items)} articles")
                    return news_items
                elif response.status == 429:
                    logger.warning("⚠️  NewsAPI rate limit")
                    return []
                else:
                    return []
        except Exception as e:
            logger.error(f"NewsAPI error: {e}")
            return []
    
    async def fetch_cryptopanic(self, session: aiohttp.ClientSession) -> List[NewsItem]:
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {
            "auth_token": "free",
            "kind": "news",
            "filter": "rising",
            "currencies": "BTC,ETH,SOL"
        }
        
        try:
            async with session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    posts = data.get("results", [])
                    news_items = []
                    for post in posts[:15]:
                        try:
                            news_item = NewsItem(
                                title=post.get("title", ""),
                                description=post.get("title", ""),
                                source=post.get("source", {}).get("title", "CryptoPanic"),
                                published_at=datetime.fromisoformat(post["created_at"].replace("Z", "+00:00")),
                                url=post.get("url", ""),
                                relevance_score=self._calculate_relevance({"title": post.get("title", "")})
                            )
                            news_items.append(news_item)
                        except:
                            continue
                    logger.info(f"✅ CryptoPanic: {len(news_items)} posts")
                    return news_items
                else:
                    return []
        except Exception as e:
            logger.error(f"CryptoPanic error: {e}")
            return []
    
    def _calculate_relevance(self, article: Dict) -> float:
        text = f"{article.get('title', '')} {article.get('description', '')}".lower()
        score = 0.0
        for keyword in self.high_priority_keywords:
            if keyword.lower() in text:
                score += 0.15
        return min(score, 1.0)
    
    async def scan(self) -> List[NewsItem]:
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                self.fetch_newsapi(session),
                self.fetch_cryptopanic(session),
                return_exceptions=True
            )
            all_news = []
            for result in results:
                if isinstance(result, list):
                    all_news.extend(result)
            seen_titles = set()
            unique_news = []
            for item in all_news:
                if item.title not in seen_titles:
                    seen_titles.add(item.title)
                    unique_news.append(item)
            unique_news.sort(key=lambda x: (x.relevance_score, x.published_at), reverse=True)
            self.cache = unique_news
            return unique_news
    
    def get_latest(self, count: int = 10) -> List[NewsItem]:
        return self.cache[:count]