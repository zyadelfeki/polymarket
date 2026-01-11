#!/usr/bin/env python3
"""
Latency Test: Binance WebSocket -> Order Placement

CRITICAL TEST: Measures end-to-end latency for latency arbitrage strategy.

Success Criteria: < 200ms from Binance price update to internal order placement

If this fails, the strategy is not viable.
"""

import asyncio
import time
from datetime import datetime
from decimal import Decimal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data_feeds.binance_websocket_v2 import BinanceWebSocketV2
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from database.ledger_async import AsyncLedger
from services.execution_service_v2 import ExecutionServiceV2


class LatencyTest:
    """Measures actual latency from price update to order placement."""
    
    def __init__(self):
        self.websocket = None
        self.execution = None
        self.ledger = None
        self.api_client = None
        
        self.measurements = []
        self.test_complete = asyncio.Event()
    
    async def initialize(self):
        """Initialize components."""
        print("[TEST] Initializing components...")
        print("="*60)
        
        # 1. Database
        self.ledger = AsyncLedger(db_path=":memory:", pool_size=5)
        await self.ledger.initialize()
        await self.ledger.record_deposit(Decimal('10000'), "Test capital")
        print(f"[✓] Ledger initialized with $10,000")
        
        # 2. API Client (paper trading)
        self.api_client = PolymarketClientV2(
            private_key=None,
            paper_trading=True,
            rate_limit=10.0
        )
        print(f"[✓] API Client initialized (paper trading)")
        
        # 3. Execution Service
        self.execution = ExecutionServiceV2(
            polymarket_client=self.api_client,
            ledger=self.ledger
        )
        await self.execution.start()
        print(f"[✓] Execution Service started")
        
        # 4. WebSocket with callback
        self.websocket = BinanceWebSocketV2(
            symbols=['BTC'],
            on_price_update=self._on_price_update
        )
        await self.websocket.start()
        print(f"[✓] Binance WebSocket started")
        
        print("="*60)
        print("[TEST] All components initialized\n")
    
    async def _on_price_update(self, symbol: str, price_data):
        """Called when WebSocket receives price update."""
        if symbol != 'BTC':
            return
        
        if len(self.measurements) >= 5:
            if not self.test_complete.is_set():
                self.test_complete.set()
            return
        
        # TIMESTAMP 1: Price update received
        t1_ns = time.time_ns()
        t1 = datetime.utcnow()
        
        print(f"\n{'='*60}")
        print(f"[TICK #{len(self.measurements) + 1}] Binance price update received")
        print(f"  Symbol: {symbol}")
        print(f"  Price: ${price_data.price}")
        print(f"  Timestamp: {t1.isoformat()}")
        
        try:
            market_id = "market_btc_100k"
            token_id = "token_yes"
            side = "YES"
            quantity = Decimal('10')
            price = Decimal('0.55')
            
            # TIMESTAMP 2: Before order placement
            t2_ns = time.time_ns()
            
            # Place order through execution service
            result = await self.execution.place_order(
                strategy="latency_arb",
                market_id=market_id,
                token_id=token_id,
                side=side,
                quantity=quantity,
                price=price,
                metadata={'test': True, 'trigger_price': float(price_data.price)}
            )
            
            # TIMESTAMP 3: After order placement
            t3_ns = time.time_ns()
            t3 = datetime.utcnow()
            
            latency_total_ms = (t3_ns - t1_ns) / 1_000_000
            latency_order_ms = (t3_ns - t2_ns) / 1_000_000
            
            measurement = {
                'tick': len(self.measurements) + 1,
                'price_timestamp': t1,
                'order_timestamp': t3,
                'latency_total_ms': latency_total_ms,
                'latency_order_ms': latency_order_ms,
                'order_success': result.success,
                'order_id': result.order_id
            }
            self.measurements.append(measurement)
            
            print(f"\n  [ORDER] Execution complete")
            print(f"    Status: {'✓ SUCCESS' if result.success else '✗ FAILED'}")
            print(f"    Order ID: {result.order_id}")
            print(f"\n  [LATENCY] Measurements:")
            print(f"    Tick->Order Total: {latency_total_ms:.2f} ms")
            print(f"    Order Execution:   {latency_order_ms:.2f} ms")
            
            if latency_total_ms > 200:
                print(f"\n  [✗] LATENCY TOO HIGH (>{latency_total_ms:.2f} ms > 200 ms threshold)")
                print(f"      Strategy will NOT be viable in production!")
            else:
                print(f"\n  [✓] Latency acceptable ({latency_total_ms:.2f} ms < 200 ms threshold)")
            
        except Exception as e:
            print(f"\n  [✗] ERROR during order placement: {e}")
            print(f"      Error type: {type(e).__name__}")
    
    async def run(self, duration_seconds: int = 30):
        print("\n" + "="*60)
        print("LATENCY TEST STARTED")
        print("="*60)
        print(f"Test Duration: {duration_seconds} seconds")
        print(f"Measurements: 5 ticks")
        print(f"Success Threshold: < 200 ms")
        print("="*60)
        print("\nWaiting for Binance WebSocket price updates...\n")
        
        try:
            await asyncio.wait_for(
                self.test_complete.wait(),
                timeout=duration_seconds
            )
        except asyncio.TimeoutError:
            print(f"\n[✗] Test timed out after {duration_seconds} seconds")
            if len(self.measurements) == 0:
                print(f"[✗] No price updates received - WebSocket connection issue?")
        
        await self._print_summary()
    
    async def _print_summary(self):
        print("\n" + "="*60)
        print("LATENCY TEST SUMMARY")
        print("="*60)
        
        if len(self.measurements) == 0:
            print("[✗] NO MEASUREMENTS COLLECTED")
            print("    - Check WebSocket connection")
            print("    - Verify Binance API is reachable")
            print("="*60)
            return
        
        latencies = [m['latency_total_ms'] for m in self.measurements]
        avg_latency = sum(latencies) / len(latencies)
        min_latency = min(latencies)
        max_latency = max(latencies)
        
        success_count = sum(1 for m in self.measurements if m['order_success'])
        success_rate = (success_count / len(self.measurements)) * 100
        
        print(f"\nMeasurements: {len(self.measurements)}")
        print(f"\n{'Tick':<6} {'Latency (ms)':<15} {'Status':<10} {'Order ID':<15}")
        print("-" * 60)
        
        for m in self.measurements:
            status = "✓ SUCCESS" if m['order_success'] else "✗ FAILED"
            latency_str = f"{m['latency_total_ms']:.2f}"
            
            if m['latency_total_ms'] <= 200:
                latency_display = f"{latency_str} (✓)"
            else:
                latency_display = f"{latency_str} (✗)"
            
            print(f"{m['tick']:<6} {latency_display:<15} {status:<10} {m['order_id'] or 'N/A':<15}")
        
        print("-" * 60)
        print(f"\nStatistics:")
        print(f"  Average Latency: {avg_latency:.2f} ms")
        print(f"  Min Latency:     {min_latency:.2f} ms")
        print(f"  Max Latency:     {max_latency:.2f} ms")
        print(f"  Success Rate:    {success_rate:.1f}%")
        
        print(f"\n" + "="*60)
        if avg_latency <= 200 and success_rate >= 80:
            print("[✓] TEST PASSED")
            print(f"    Average latency ({avg_latency:.2f} ms) is acceptable")
            print(f"    Success rate ({success_rate:.1f}%) is acceptable")
            print("\n    → Strategy is VIABLE for production")
        else:
            print("[✗] TEST FAILED")
            if avg_latency > 200:
                print(f"    Average latency ({avg_latency:.2f} ms) exceeds 200 ms threshold")
                print("    → Strategy is NOT viable - too slow")
            if success_rate < 80:
                print(f"    Success rate ({success_rate:.1f}%) is below 80% threshold")
                print("    → Execution service has reliability issues")
        
        print("="*60)
    
    async def cleanup(self):
        print("\n[TEST] Cleaning up...")
        
        if self.execution:
            await self.execution.stop()
        
        if self.websocket:
            await self.websocket.stop()
        
        if self.api_client:
            await self.api_client.close()
        
        if self.ledger:
            await self.ledger.close()
        
        print("[✓] Cleanup complete")


async def main():
    test = LatencyTest()
    
    try:
        await test.initialize()
        await test.run(duration_seconds=30)
    
    except KeyboardInterrupt:
        print("\n[!] Test interrupted by user")
    
    except Exception as e:
        print(f"\n[✗] Test failed with error: {e}")
        print(f"    Error type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
    
    finally:
        await test.cleanup()


if __name__ == '__main__':
    print("\n" + "="*60)
    print("POLYMARKET LATENCY TEST")
    print("="*60)
    print("\nThis test measures end-to-end latency:")
    print("  1. Binance WebSocket price update received")
    print("  2. Order placed via ExecutionServiceV2")
    print("  3. Latency measured in milliseconds")
    print("\nCritical Threshold: < 200 ms")
    print("\nStarting test...\n")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nTest terminated by user.")
    
    print("\n" + "="*60)
    print("TEST COMPLETE")
    print("="*60 + "\n")
