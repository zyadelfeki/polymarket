import os
import sys
import pytest
from datetime import datetime

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data_feeds.news_monitor_v1 import NewsMonitor


@pytest.mark.asyncio
async def test_parse_rss_tweets():
    monitor = NewsMonitor()
    rss = """
    <rss><channel>
      <item><title>Breaking: SEC approves Bitcoin ETF</title><pubDate>Mon, 01 Jan 2026 12:00:00 GMT</pubDate></item>
      <item><title>Markets steady amid Fed announcement</title><pubDate>Mon, 01 Jan 2026 13:00:00 GMT</pubDate></item>
    </channel></rss>
    """

    tweets = monitor._parse_rss_tweets(rss)
    assert len(tweets) == 2


@pytest.mark.asyncio
async def test_analyze_tweet_generates_alert():
    monitor = NewsMonitor()
    alert = await monitor._analyze_tweet(
        text="SEC approves Bitcoin ETF, markets surge",
        timestamp=datetime.utcnow(),
        source="Reuters"
    )

    assert alert is not None
    assert alert.predicted_direction in {"UP", "DOWN", "UNCLEAR"}
    assert "SEC" in alert.keywords
