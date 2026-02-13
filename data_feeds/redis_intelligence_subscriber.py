import json
import logging
import time
from typing import Dict, Optional

import redis

logger = logging.getLogger(__name__)


class RedisIntelligenceSubscriber:
    def __init__(self, host: str = "localhost", port: int = 6379) -> None:
        self.redis_client = redis.Redis(host=host, port=port, decode_responses=True)

    def get_intelligence(self) -> Optional[Dict]:
        data_str = self.redis_client.get("charlie:intelligence")
        if not data_str:
            return None

        data = json.loads(data_str)

        age = time.time() - data["timestamp"]
        if age > 30:
            logger.warning("stale_intelligence | age=%ss", int(age))
            return None

        return data
