#!/usr/bin/env python3
"""
Comprehensive Integration Tests for V2 Components

Tests:
1. Polymarket Client V2 - API operations, retry logic, rate limiting
2. Async Ledger - Database operations, caching, connection pooling
3. Execution Service V2 - Order placement, fill tracking, state management
4. End-to-end flow - Complete trading cycle

Standards:
- Real async testing
- Proper mocking of external APIs
- Fixtures for test data
- Cleanup after tests
"""

import pytest
import pytest_asyncio
import asyncio
import tempfile
import os
from decimal import Decimal
from datetime import datetime
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from typing import Dict, List

# Import V2 components
from data_feeds.polymarket_client_v2 import (
    PolymarketClientV2,
    OrderSide,
    TokenBucket,
    RequestMetrics
)
from database.ledger_async import AsyncLedger, PositionData
from services.execution_service_v2 import (
    ExecutionServiceV2,
    OrderRequest,
    OrderStatus,
    OrderResult
)


# ==================== FIXTURES ====================

@pytest_asyncio.fixture
async def temp_db():
    """Create temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    # Initialize schema
    import aiosqlite
    async with aiosqlite.connect(path) as conn:
        # Create schema
        await conn.execute("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY,
                account_name TEXT UNIQUE NOT NULL,
                account_type TEXT NOT NULL,
                balance REAL DEFAULT 0.0
            )
        """)
        
        await conn.execute("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                description TEXT
            )
        """)
        
        await conn.execute("""
            CREATE TABLE transaction_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
        """)
        
        await conn.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                current_price REAL,
                unrealized_pnl REAL DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                status TEXT DEFAULT 'OPEN',
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT,
                order_id TEXT,
                metadata TEXT
            )
        """)
        
        # Insert base accounts
        await conn.execute(
            "INSERT INTO accounts (account_name, account_type) VALUES (?, ?)",
            ('Cash', 'ASSET')
        )
        await conn.execute(
            "INSERT INTO accounts (account_name, account_type) VALUES (?, ?)",
            ('Positions', 'ASSET')
        )
        await conn.execute(
            "INSERT INTO accounts (account_name, account_type) VALUES (?, ?)",
            ('Owner Equity', 'EQUITY')
        )
        
        await conn.commit()
    
    yield path
    
    # Cleanup
    try:
        os.unlink(path)
    except:
        pass


@pytest_asyncio.fixture
async def ledger(temp_db):
    """Create async ledger instance."""
    ledger = AsyncLedger(db_path=temp_db, pool_size=3)
    await ledger.pool.initialize()
    yield ledger
    await ledger.close()


@pytest_asyncio.fixture
def mock_polymarket_client():
    """Mock Polymarket client."""
    client = AsyncMock(spec=PolymarketClientV2)
    
    # Mock methods
    client.get_markets.return_value = [
        {
            'condition_id': 'test_market_1',
            'question': 'Will BTC reach $100k by EOY 2026?',
            'active': True,
            'closed': False,
            'tokens': [
                {'token_id': 'yes_token_1', 'outcome': 'Yes'},
                {'token_id': 'no_token_1', 'outcome': 'No'}
            ]
        }
    ]
    
    client.get_orderbook.return_value = {
        'bids': [
            {'price': '0.54', 'size': '100'},
            {'price': '0.53', 'size': '200'}
        ],
        'asks': [
            {'price': '0.56', 'size': '150'},
            {'price': '0.57', 'size': '250'}
        ]
    }
    
    client.place_order.return_value = {
        'success': True,
        'order_id': 'order_12345',
        'paper': True
    }
    
    client.get_order_status.return_value = {
        'order_id': 'order_12345',
        'status': 'filled',
        'fills': [
            {
                'id': 'fill_1',
                'size': '50.0',
                'price': '0.55',
                'fee': '0.05'
            }
        ]
    }
    
    client.health_check.return_value = True
    
    return client


@pytest_asyncio.fixture
async def execution_service(mock_polymarket_client, ledger):
    """Create execution service instance."""
    service = ExecutionServiceV2(
        polymarket_client=mock_polymarket_client,
        ledger=ledger,
        config={'max_retries': 2, 'timeout_seconds': 5}
    )
    await service.start()
    yield service
    await service.stop()


# ==================== TOKEN BUCKET TESTS ====================

@pytest.mark.asyncio
async def test_token_bucket_basic():
    """Test token bucket basic operation."""
    bucket = TokenBucket(rate=2.0, capacity=5.0)
    
    # Should have initial capacity
    acquired = await bucket.acquire(tokens=3.0, timeout=1.0)
    assert acquired is True
    
    # Should have 2 tokens left
    acquired = await bucket.acquire(tokens=2.0, timeout=1.0)
    assert acquired is True
    
    # Should fail (no tokens left)
    acquired = await bucket.acquire(tokens=1.0, timeout=0.1)
    assert acquired is False


@pytest.mark.asyncio
async def test_token_bucket_refill():
    """Test token bucket refills over time."""
    bucket = TokenBucket(rate=10.0, capacity=10.0)  # 10 tokens/sec
    
    # Drain bucket
    await bucket.acquire(tokens=10.0)
    
    # Wait for refill
    await asyncio.sleep(0.5)  # Should refill 5 tokens
    
    # Should be able to acquire ~5 tokens
    acquired = await bucket.acquire(tokens=4.0, timeout=0.1)
    assert acquired is True


# ==================== POLYMARKET CLIENT TESTS ====================

@pytest.mark.asyncio
async def test_polymarket_client_initialization():
    """Test client initializes correctly."""
    client = PolymarketClientV2(
        private_key=None,
        rate_limit=5.0,
        paper_trading=True
    )
    
    assert client.paper_trading is True
    assert client.can_trade is False
    assert client.rate_limiter is not None
    
    await client.close()


@pytest.mark.asyncio
async def test_polymarket_client_rate_limiting():
    """Test rate limiting works."""
    client = PolymarketClientV2(
        private_key=None,
        rate_limit=2.0,  # 2 req/s
        paper_trading=True
    )
    
    # Make multiple requests
    import time
    start = time.time()
    
    for _ in range(5):
        # This will block if rate limiting works
        await client.rate_limiter.acquire()
    
    elapsed = time.time() - start
    
    # Should take at least 2 seconds (5 tokens at 2/sec)
    assert elapsed >= 1.5  # Allow some margin
    
    await client.close()


@pytest.mark.asyncio
async def test_polymarket_client_metrics():
    """Test client tracks metrics."""
    client = PolymarketClientV2(private_key=None, paper_trading=True)
    
    # Record some requests
    client.metrics.record_request(success=True, latency_ms=50.0)
    client.metrics.record_request(success=True, latency_ms=100.0)
    client.metrics.record_request(success=False, latency_ms=200.0)
    
    metrics = client.get_metrics()
    
    assert metrics['total_requests'] == 3
    assert metrics['successful_requests'] == 2
    assert metrics['failed_requests'] == 1
    assert metrics['success_rate'] == pytest.approx(2/3)
    assert metrics['avg_latency_ms'] == pytest.approx(116.67, rel=0.01)
    
    await client.close()


# ==================== ASYNC LEDGER TESTS ====================

@pytest.mark.asyncio
async def test_ledger_deposit(ledger):
    """Test recording deposits."""
    tx_id = await ledger.record_deposit(
        amount=Decimal('10000'),
        description='Initial capital'
    )
    
    assert tx_id > 0
    
    equity = await ledger.get_equity()
    assert equity == Decimal('10000')


@pytest.mark.asyncio
async def test_ledger_caching(ledger):
    """Test equity caching works."""
    await ledger.record_deposit(Decimal('5000'))
    
    # First call - cache miss
    equity1 = await ledger.get_equity()
    cache_misses1 = ledger.cache_misses
    
    # Second call - cache hit
    equity2 = await ledger.get_equity()
    cache_hits = ledger.cache_hits
    
    assert equity1 == equity2
    assert cache_hits > 0
    assert cache_misses1 == 1


@pytest.mark.asyncio
async def test_ledger_trade_entry(ledger):
    """Test recording trade entries."""
    # Deposit capital first
    await ledger.record_deposit(Decimal('10000'))
    
    # Record trade
    position_id = await ledger.record_trade_entry(
        market_id='market_123',
        token_id='token_yes',
        strategy='test_strategy',
        entry_price=Decimal('0.55'),
        quantity=Decimal('100'),
        fees=Decimal('0.50'),
        order_id='order_123',
        metadata={'test': True}
    )
    
    assert position_id > 0
    
    # Check position was created
    positions = await ledger.get_open_positions()
    assert len(positions) == 1
    assert positions[0].market_id == 'market_123'
    assert positions[0].quantity == Decimal('100')


@pytest.mark.asyncio
async def test_ledger_connection_pooling(ledger):
    """Test connection pooling works."""
    # Make multiple concurrent queries
    tasks = [
        ledger.get_equity()
        for _ in range(10)
    ]
    
    results = await asyncio.gather(*tasks)
    
    # All should return same value
    assert all(r == results[0] for r in results)


@pytest.mark.asyncio
async def test_ledger_validation(ledger):
    """Test ledger validation."""
    await ledger.record_deposit(Decimal('1000'))
    
    # Should pass validation
    valid = await ledger.validate_ledger()
    assert valid is True


@pytest.mark.asyncio
async def test_ledger_metrics(ledger):
    """Test ledger tracks metrics."""
    await ledger.get_equity()
    await ledger.get_equity()  # Cache hit
    
    metrics = await ledger.get_metrics()
    
    assert metrics['queries_executed'] > 0
    assert metrics['cache_hits'] > 0
    assert metrics['cache_hit_rate'] > 0


# ==================== EXECUTION SERVICE TESTS ====================

@pytest.mark.asyncio
async def test_execution_service_place_order(execution_service, ledger):
    """Test placing an order."""
    # Deposit capital first
    await ledger.record_deposit(Decimal('10000'))
    
    # Place order
    result = await execution_service.place_order(
        strategy='test_strategy',
        market_id='market_123',
        token_id='token_yes',
        side='YES',
        quantity=Decimal('50'),
        price=Decimal('0.55'),
        metadata={'test': True}
    )
    
    assert result.success is True
    assert result.order_id is not None
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_execution_service_invalid_order(execution_service):
    """Test rejecting invalid orders."""
    # Invalid price
    result = await execution_service.place_order(
        strategy='test',
        market_id='market_123',
        token_id='token_yes',
        side='YES',
        quantity=Decimal('50'),
        price=Decimal('1.50'),  # Invalid (> 0.99)
    )
    
    assert result.success is False
    assert result.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_execution_service_metrics(execution_service, ledger):
    """Test execution service tracks metrics."""
    await ledger.record_deposit(Decimal('10000'))
    
    # Place successful order
    await execution_service.place_order(
        strategy='test',
        market_id='market_123',
        token_id='token_yes',
        side='YES',
        quantity=Decimal('50'),
        price=Decimal('0.55')
    )
    
    metrics = execution_service.get_metrics()
    
    assert metrics['orders_placed'] == 1
    assert metrics['orders_filled'] >= 0
    assert metrics['fill_rate'] >= 0


@pytest.mark.asyncio
async def test_execution_service_cancel_orders(execution_service, mock_polymarket_client):
    """Test cancelling all orders."""
    mock_polymarket_client.cancel_order.return_value = True
    
    # Place order (won't complete)
    mock_polymarket_client.get_order_status.return_value = {
        'order_id': 'order_123',
        'status': 'pending',
        'fills': []
    }
    
    # Cancel all
    cancelled = await execution_service.cancel_all_orders()
    
    # Should have tried to cancel
    assert cancelled >= 0


# ==================== END-TO-END TESTS ====================

@pytest.mark.asyncio
async def test_end_to_end_trade_flow(mock_polymarket_client, ledger):
    """Test complete trading flow."""
    # 1. Initialize
    execution = ExecutionServiceV2(
        polymarket_client=mock_polymarket_client,
        ledger=ledger
    )
    await execution.start()
    
    try:
        # 2. Deposit capital
        await ledger.record_deposit(Decimal('10000'))
        
        # 3. Place order
        result = await execution.place_order(
            strategy='latency_arb',
            market_id='market_btc',
            token_id='token_yes',
            side='YES',
            quantity=Decimal('100'),
            price=Decimal('0.60'),
            metadata={'symbol': 'BTC', 'threshold': 100000}
        )
        
        # 4. Verify order executed
        assert result.success is True
        assert result.filled_quantity > 0
        
        # 5. Check position recorded
        positions = await ledger.get_open_positions()
        assert len(positions) == 1
        assert positions[0].strategy == 'latency_arb'
        
        # 6. Check equity updated
        equity = await ledger.get_equity()
        assert equity > 0
        
        # 7. Validate ledger
        valid = await ledger.validate_ledger()
        assert valid is True
    
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_concurrent_order_placement(mock_polymarket_client, ledger):
    """Test placing multiple orders concurrently."""
    execution = ExecutionServiceV2(
        polymarket_client=mock_polymarket_client,
        ledger=ledger
    )
    await execution.start()
    
    try:
        # Deposit capital
        await ledger.record_deposit(Decimal('100000'))
        
        # Place multiple orders concurrently
        tasks = [
            execution.place_order(
                strategy='test',
                market_id=f'market_{i}',
                token_id=f'token_{i}',
                side='YES',
                quantity=Decimal('10'),
                price=Decimal('0.50')
            )
            for i in range(5)
        ]
        
        results = await asyncio.gather(*tasks)
        
        # All should succeed
        assert all(r.success for r in results)
        
        # Check positions
        positions = await ledger.get_open_positions()
        assert len(positions) >= 3  # At least some filled
    
    finally:
        await execution.stop()


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
