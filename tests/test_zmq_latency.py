"""
Benchmark ZMQ latency vs file-based IPC
"""
import asyncio
import json
import time
import tempfile
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from data_feeds.zmq_price_subscriber import ZMQPriceSubscriber


async def benchmark_zmq_latency() -> None:
    """Measure ZMQ receive latency."""
    print("\n" + "=" * 60)
    print("📊 ZMQ LATENCY BENCHMARK")
    print("=" * 60)

    subscriber = ZMQPriceSubscriber(
        connect_address="tcp://127.0.0.1:5555",
        staleness_threshold=1.0,
    )

    listener_task = asyncio.create_task(subscriber.start_listening())

    print("⏳ Waiting for first price update...")
    await asyncio.sleep(2.0)

    latencies = []
    samples = 100

    print(f"\n📡 Collecting {samples} latency samples...")

    for i in range(samples):
        price = subscriber.get_price()

        if price and subscriber.latest_timestamp:
            age = time.time() - subscriber.latest_timestamp
            latency_ms = age * 1000
            latencies.append(latency_ms)

            if i % 10 == 0:
                print(f"  Sample {i+1}/{samples}: {latency_ms:.3f}ms")

        await asyncio.sleep(0.1)

    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        min_latency = min(latencies)
        max_latency = max(latencies)

        print("\n" + "=" * 60)
        print("📊 RESULTS")
        print("=" * 60)
        print(f"Samples collected: {len(latencies)}")
        print(f"Average latency:   {avg_latency:.3f}ms")
        print(f"Minimum latency:   {min_latency:.3f}ms")
        print(f"Maximum latency:   {max_latency:.3f}ms")
        print("=" * 60)

        if avg_latency < 5.0:
            print("✅ PASS: Average latency < 5ms target")
        else:
            print(f"⚠️  WARN: Average latency {avg_latency:.2f}ms exceeds 5ms target")

        print("=" * 60)
    else:
        print("❌ FAIL: No latency samples collected")
        print("Is the test publisher running?")

    listener_task.cancel()


async def benchmark_file_io_latency() -> None:
    """Measure file-based IPC latency for comparison."""
    print("\n" + "=" * 60)
    print("📊 FILE I/O LATENCY BENCHMARK (Baseline)")
    print("=" * 60)

    latencies = []
    samples = 100

    with tempfile.TemporaryDirectory() as tmpdir:
        price_file = Path(tmpdir) / "btc_price.json"

        print(f"\n📁 Writing/reading {samples} times from disk...")

        for i in range(samples):
            write_start = time.time()
            data = {
                "symbol": "BTC/USDT",
                "price": 96000.0,
                "timestamp": time.time(),
            }
            price_file.write_text(json.dumps(data))

            _ = json.loads(price_file.read_text())
            read_end = time.time()

            latency_ms = (read_end - write_start) * 1000
            latencies.append(latency_ms)

            if i % 10 == 0:
                print(f"  Sample {i+1}/{samples}: {latency_ms:.3f}ms")

            await asyncio.sleep(0.1)

    avg_latency = sum(latencies) / len(latencies)
    min_latency = min(latencies)
    max_latency = max(latencies)

    print("\n" + "=" * 60)
    print("📊 RESULTS")
    print("=" * 60)
    print(f"Samples collected: {len(latencies)}")
    print(f"Average latency:   {avg_latency:.3f}ms")
    print(f"Minimum latency:   {min_latency:.3f}ms")
    print(f"Maximum latency:   {max_latency:.3f}ms")
    print("=" * 60)


async def main() -> None:
    print("\n🔬 PHASE 2 LATENCY VALIDATION")
    print("Testing ZMQ vs File-based IPC performance\n")

    await benchmark_file_io_latency()
    await benchmark_zmq_latency()

    print("\n✅ Benchmark complete")


if __name__ == "__main__":
    asyncio.run(main())
