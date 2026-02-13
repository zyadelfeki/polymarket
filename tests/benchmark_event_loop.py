"""Benchmark asyncio vs uvloop event loop performance"""
import asyncio
import time
from decimal import Decimal
import sys


async def simulate_trading_loop(iterations=1000):
    """Simulate typical trading bot operations"""
    start = time.time()

    for i in range(iterations):
        # Simulate market data processing
        price = Decimal("96000") + Decimal(str(i))

        # Simulate async I/O operations
        await asyncio.sleep(0.001)  # 1ms delay per iteration

        # Simulate calculation
        edge = price * Decimal("0.02")
        kelly_size = edge * Decimal("0.25")
        _ = kelly_size

    elapsed = time.time() - start
    return elapsed, iterations


async def main():
    print("=" * 60)
    print("EVENT LOOP BENCHMARK")
    print("=" * 60)

    # Test with current event loop (should be uvloop after install)
    elapsed, iterations = await simulate_trading_loop(1000)
    ops_per_sec = iterations / elapsed

    print(f"Iterations: {iterations}")
    print(f"Time elapsed: {elapsed:.3f}s")
    print(f"Operations/sec: {ops_per_sec:.1f}")
    print(f"Avg time per iteration: {(elapsed/iterations)*1000:.3f}ms")
    print("=" * 60)

    loop = asyncio.get_event_loop()
    loop_type = type(loop).__name__
    print(f"Event loop type: {loop_type}")

    if 'uvloop' in loop_type.lower():
        print("✅ uvloop is ACTIVE")
    else:
        print("⚠️  Standard asyncio (uvloop not active)")

    print("=" * 60)


if __name__ == "__main__":
    # Run WITHOUT uvloop first
    print("\n🔹 Baseline (standard asyncio):")
    asyncio.run(main())

    # Run WITH uvloop
    print("\n🔹 With uvloop optimization:")
    try:
        if sys.platform == "win32":
            raise RuntimeError("uvloop unsupported on Windows")
        import uvloop

        uvloop.install()
        asyncio.run(main())
    except Exception as exc:
        print(f"⚠️  uvloop not available: {exc}")
