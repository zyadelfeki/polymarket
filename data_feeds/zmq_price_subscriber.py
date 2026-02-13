import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

import msgpack
import zmq

logger = logging.getLogger(__name__)


class ZMQPriceSubscriber:
    """Subscribe to ZMQ price feed."""

    def __init__(
        self,
        connect_address: str = "tcp://127.0.0.1:5555",
        staleness_threshold: float = 1.0,
    ) -> None:
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.connect(connect_address)
        self.socket.setsockopt(zmq.SUBSCRIBE, b"")
        self.staleness_threshold = staleness_threshold
        self.latest_price: Optional[Decimal] = None
        self.latest_timestamp: Optional[float] = None
        logger.info("zmq_subscriber_connected | address=%s", connect_address)

    async def start_listening(self) -> None:
        """Run in background asyncio task."""
        while True:
            try:
                if self.socket.poll(timeout=100):
                    payload = self.socket.recv()
                    data = msgpack.unpackb(payload, raw=False)

                    self.latest_price = Decimal(str(data["price"]))
                    self.latest_timestamp = data["timestamp"]

                    logger.debug(
                        "price_update | symbol=%s price=%s",
                        data.get("symbol"),
                        str(self.latest_price),
                    )

                await asyncio.sleep(0.001)

            except Exception as exc:
                logger.error("zmq_receive_error | error=%s", str(exc))
                await asyncio.sleep(1)

    def get_price(self) -> Optional[Decimal]:
        """Get latest price with staleness check."""
        if self.latest_price is None or self.latest_timestamp is None:
            return None

        age = time.time() - self.latest_timestamp
        if age > self.staleness_threshold:
            logger.error("stale_price | age=%.2fs", age)
            return None

        return self.latest_price
