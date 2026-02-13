"""
Test ZMQ publisher to simulate Charlie bot
Run this BEFORE starting main_capital_doubler.py for testing
"""
import sys
import time
from decimal import Decimal

import msgpack
import zmq


def main() -> None:
    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind("tcp://127.0.0.1:5555")

    print("=" * 60)
    print("🚀 TEST ZMQ PUBLISHER STARTED")
    print("=" * 60)
    print("Publishing BTC prices on tcp://127.0.0.1:5555")
    print("Simulating real-time price feed from Binance")
    print("Press Ctrl+C to stop")
    print("=" * 60)

    base_price = 96000
    iteration = 0

    try:
        while True:
            price_change = (iteration % 20 - 10) * 50
            current_price = Decimal(str(base_price + price_change))

            publish_time = time.time()
            data = {
                "symbol": "BTC/USDT",
                "price": float(current_price),
                "timestamp": publish_time,
            }

            payload = msgpack.packb(data)
            socket.send(payload)

            print(
                f"[{iteration:04d}] Published: ${current_price:,.2f} at {publish_time:.6f}"
            )

            iteration += 1
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n" + "=" * 60)
        print(f"✅ Published {iteration} price updates")
        print("🛑 Test publisher stopped")
        print("=" * 60)
        socket.close()
        context.term()
        sys.exit(0)


if __name__ == "__main__":
    main()
