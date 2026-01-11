# 🚀 PAPER TRADING LAUNCH GUIDE

**Status:** Ready to Launch  
**Target:** 3-4 Days to Paper Trading  
**System Readiness:** 90%

---

## 📋 PRE-LAUNCH CHECKLIST

### Phase 1: Environment Setup (1 hour)

#### 1.1 Install Dependencies
```bash
# Install all requirements
pip install -r requirements.txt

# Verify installation
python -c "import pydantic, structlog, aiosqlite; print('✅ All dependencies installed')"
```
- [ ] All dependencies installed
- [ ] Python 3.10+ verified
- [ ] Virtual environment activated

#### 1.2 Configure Secrets
```bash
# Set encryption password for local secrets
export SECRETS_ENCRYPTION_KEY="your_secure_password_here"

# Or use AWS Secrets Manager
export SECRETS_BACKEND=aws
export AWS_REGION=us-east-1
```
- [ ] Secrets backend configured
- [ ] Encryption key set (if using local)
- [ ] AWS credentials configured (if using AWS)

#### 1.3 Set API Credentials
```bash
# For paper trading, you can use test keys or empty values
# The system will simulate orders

# Option 1: Environment variables
export POLYMARKET_API_KEY="test_key"
export POLYMARKET_PRIVATE_KEY="0x0000000000000000000000000000000000000000000000000000000000000000"

# Option 2: Encrypted local file
python -c "
from security.secrets_manager import SecretsManager
import asyncio

async def setup():
    manager = SecretsManager(
        backend='local',
        encryption_key='your_password'
    )
    await manager.set_secret('polymarket_api_key', 'test_key')
    await manager.set_secret('polymarket_private_key', '0x' + '0'*64)
    print('✅ Secrets configured')

asyncio.run(setup())
"
```
- [ ] API credentials set
- [ ] Private key configured (test key for paper trading)
- [ ] Secrets verified accessible

#### 1.4 Initialize Database
```bash
# Create data directory
mkdir -p data/backups logs

# Initialize database with schema
sqlite3 data/trading.db < database/schema_v2.sql

# Verify schema
sqlite3 data/trading.db "SELECT name FROM sqlite_master WHERE type='table';"
```
- [ ] Database created
- [ ] Schema applied
- [ ] Tables verified
- [ ] Initial accounts created

#### 1.5 Configuration Review
```bash
# Copy production config
cp config/production.yaml config/current.yaml

# Review settings
cat config/current.yaml

# Validate configuration
python -c "
from validation.models import TradingConfig
import yaml

with open('config/current.yaml') as f:
    config = yaml.safe_load(f)

try:
    trading_config = TradingConfig(**config['trading'])
    print('✅ Configuration valid')
except Exception as e:
    print(f'❌ Configuration error: {e}')
"
```
- [ ] Configuration copied
- [ ] Settings reviewed
- [ ] Validation passed
- [ ] Paper trading mode enabled

---

### Phase 2: System Testing (2-4 hours)

#### 2.1 Run Integration Tests
```bash
# Run all integration tests
pytest tests/test_integration_v2.py -v

# Expected: All tests pass
```
- [ ] All 25+ tests pass
- [ ] No errors in output
- [ ] Test database cleaned up

#### 2.2 Component Health Checks
```bash
# Test database connection
python -c "
import asyncio
from database.ledger_async import AsyncLedger
from decimal import Decimal

async def test():
    ledger = AsyncLedger('data/trading.db')
    await ledger.pool.initialize()
    
    # Deposit test capital
    tx_id = await ledger.record_deposit(Decimal('10000'), 'Initial capital')
    
    # Verify
    equity = await ledger.get_equity()
    print(f'✅ Database OK - Equity: ${equity}')
    
    await ledger.close()

asyncio.run(test())
"
```
- [ ] Database connection works
- [ ] Can write transactions
- [ ] Can read equity
- [ ] Connection pool works

```bash
# Test WebSocket connection
python -c "
import asyncio
from data_feeds.binance_websocket_v2 import BinanceWebSocketV2

async def test():
    ws = BinanceWebSocketV2(symbols=['BTC', 'ETH'])
    await ws.start()
    
    # Wait for data
    await asyncio.sleep(5)
    
    # Check price
    btc_price = await ws.get_price('BTC')
    print(f'✅ WebSocket OK - BTC: ${btc_price}')
    
    await ws.stop()

asyncio.run(test())
"
```
- [ ] WebSocket connects
- [ ] Receives price data
- [ ] Auto-reconnects on disconnect
- [ ] Heartbeat works

```bash
# Test API client
python -c "
import asyncio
from data_feeds.polymarket_client_v2 import PolymarketClientV2

async def test():
    client = PolymarketClientV2(paper_trading=True)
    
    # Health check
    healthy = await client.health_check()
    print(f'✅ API Client OK - Healthy: {healthy}')
    
    # Get metrics
    metrics = client.get_metrics()
    print(f'   Metrics: {metrics}')
    
    await client.close()

asyncio.run(test())
"
```
- [ ] API client initializes
- [ ] Health check passes
- [ ] Rate limiting works
- [ ] Metrics tracked

#### 2.3 Circuit Breaker Test
```bash
# Test circuit breaker
python -c "
import asyncio
from risk.circuit_breaker_v2 import CircuitBreakerV2
from decimal import Decimal

async def test():
    cb = CircuitBreakerV2(initial_equity=Decimal('10000'))
    
    # Should allow trading initially
    can_trade = await cb.can_trade(Decimal('10000'))
    print(f'✅ Initial state: can_trade={can_trade}')
    
    # Simulate losses
    for i in range(5):
        await cb.record_trade_result(Decimal('-100'), is_win=False)
    
    # Should trip after 5 losses
    can_trade = await cb.can_trade(Decimal('9500'))
    print(f'✅ After 5 losses: can_trade={can_trade} (should be False)')
    
    status = cb.get_status()
    print(f'   Status: {status}')

asyncio.run(test())
"
```
- [ ] Circuit breaker initializes
- [ ] Allows trading initially
- [ ] Trips on losses
- [ ] Reports correct status

#### 2.4 Health Monitor Test
```bash
# Test health monitor
python -c "
import asyncio
from services.health_monitor_v2 import HealthMonitorV2

async def test():
    monitor = HealthMonitorV2(check_interval=5.0)
    
    # Register dummy component
    async def check_healthy():
        return True
    
    monitor.register_component('test_component', check_healthy)
    
    # Start monitoring
    await monitor.start()
    
    # Wait for checks
    await asyncio.sleep(10)
    
    # Get metrics
    metrics = monitor.get_metrics()
    print(f'✅ Health Monitor OK')
    print(f'   Checks: {metrics["total_checks"]}')
    print(f'   Alerts: {metrics["total_alerts"]}')
    
    await monitor.stop()

asyncio.run(test())
"
```
- [ ] Health monitor starts
- [ ] Performs checks
- [ ] Tracks metrics
- [ ] Stops gracefully

---

### Phase 3: Dry Run (24-48 hours)

#### 3.1 Start System
```bash
# Start the trading bot
python main.py --config config/current.yaml --mode paper

# Monitor logs in another terminal
tail -f logs/trading.log
```
- [ ] System starts without errors
- [ ] All components initialize
- [ ] Logs are structured JSON
- [ ] No warnings/errors

#### 3.2 Monitor Operations

**First Hour:**
- [ ] WebSocket connected and receiving data
- [ ] API client healthy
- [ ] Database queries < 10ms
- [ ] No memory leaks
- [ ] CPU usage reasonable (<50%)

**First 6 Hours:**
- [ ] System stable
- [ ] Health checks all passing
- [ ] No component restarts
- [ ] Equity tracked correctly
- [ ] Orders simulated properly

**First 24 Hours:**
- [ ] No crashes/restarts
- [ ] All strategies executing
- [ ] Circuit breaker responsive
- [ ] Metrics accumulating
- [ ] Logs rotating properly

**48 Hours:**
- [ ] Continuous operation
- [ ] Performance stable
- [ ] No memory leaks confirmed
- [ ] Database size reasonable
- [ ] All safety limits respected

#### 3.3 Validation Checks

```bash
# Check database integrity
sqlite3 data/trading.db "PRAGMA integrity_check;"

# Check equity calculation
python -c "
import asyncio
from database.ledger_async import AsyncLedger

async def check():
    ledger = AsyncLedger('data/trading.db')
    await ledger.pool.initialize()
    
    equity = await ledger.get_equity()
    valid = await ledger.validate_ledger()
    
    print(f'Equity: ${equity}')
    print(f'Balanced: {valid}')
    
    await ledger.close()

asyncio.run(check())
"

# Check for errors in logs
grep -i error logs/trading.log | wc -l

# Check circuit breaker status
grep circuit_breaker logs/trading.log | tail -10
```
- [ ] Database integrity OK
- [ ] Ledger balanced
- [ ] No critical errors
- [ ] Circuit breaker functional

---

### Phase 4: Go/No-Go Decision

#### ✅ GO Criteria (Must Have ALL)
1. [ ] All integration tests pass
2. [ ] 24+ hours of stable operation
3. [ ] Zero critical errors
4. [ ] All components healthy
5. [ ] Circuit breaker tested
6. [ ] Database validated
7. [ ] Performance acceptable
8. [ ] Logs clean and structured
9. [ ] Monitoring working
10. [ ] Team confident

#### ❌ NO-GO Criteria (ANY triggers abort)
1. [ ] Critical errors in logs
2. [ ] System crashes/restarts
3. [ ] Memory leaks detected
4. [ ] Database corruption
5. [ ] Circuit breaker failure
6. [ ] Health monitor not working
7. [ ] Performance degradation
8. [ ] Validation failures
9. [ ] Missing monitoring
10. [ ] Team concerns

---

## 🎯 LAUNCH DAY PROCEDURE

### Hour 0: Pre-Launch (30 minutes before)

```bash
# 1. Final system check
pytest tests/test_integration_v2.py -v

# 2. Database backup
cp data/trading.db data/backups/trading_$(date +%Y%m%d_%H%M%S).db

# 3. Verify configuration
grep paper_trading config/current.yaml  # Should be true

# 4. Clear old logs (optional)
mv logs/trading.log logs/trading_$(date +%Y%m%d_%H%M%S).log

# 5. Start system
python main.py --config config/current.yaml --mode paper
```

**Checklist:**
- [ ] Tests pass
- [ ] Backup created
- [ ] Configuration verified
- [ ] Logs archived
- [ ] System started

### Hour 1: Active Monitoring

**Monitor every 5 minutes:**
- [ ] System status (health_check)
- [ ] CPU/memory usage
- [ ] Error count (should be 0)
- [ ] Order simulation working
- [ ] Equity tracking correct

### Hour 2-6: Periodic Checks

**Monitor every 30 minutes:**
- [ ] All components healthy
- [ ] No unusual behavior
- [ ] Performance stable
- [ ] Logs clean

### Hour 6-24: Stability Monitoring

**Monitor every 2 hours:**
- [ ] System uptime
- [ ] Component status
- [ ] Database size
- [ ] Memory usage trend

### Day 2-3: Continuous Operation

**Monitor twice daily:**
- [ ] Morning check (all systems)
- [ ] Evening check (performance review)
- [ ] Log analysis (error trends)
- [ ] Metrics review (equity, orders, etc.)

---

## 🚨 EMERGENCY PROCEDURES

### If System Crashes

```bash
# 1. Check logs for error
tail -100 logs/trading.log

# 2. Check database integrity
sqlite3 data/trading.db "PRAGMA integrity_check;"

# 3. Restore from backup if needed
cp data/backups/trading_LATEST.db data/trading.db

# 4. Restart with debug logging
LOG_LEVEL=DEBUG python main.py --config config/current.yaml --mode paper
```

### If Circuit Breaker Trips

```bash
# 1. Check why it tripped
grep circuit_breaker logs/trading.log | tail -20

# 2. Review recent trades
sqlite3 data/trading.db "SELECT * FROM positions ORDER BY id DESC LIMIT 10;"

# 3. Review equity
python -c "...(check equity script)..."

# 4. Manual reset if needed (after review)
python -c "
import asyncio
from risk.circuit_breaker_v2 import CircuitBreakerV2
from decimal import Decimal

async def reset():
    cb = CircuitBreakerV2(initial_equity=Decimal('10000'))
    await cb.manual_reset()
    print('✅ Circuit breaker reset')

asyncio.run(reset())
"
```

### If Component Fails

```bash
# Health monitor should auto-restart
# If not, manual restart:

# 1. Check which component failed
grep health_check_failed logs/trading.log | tail -20

# 2. Check component status
grep component_restart logs/trading.log | tail -20

# 3. Manual intervention if auto-restart fails
# (Restart entire system)
kill -SIGTERM $(pgrep -f main.py)
python main.py --config config/current.yaml --mode paper
```

---

## 📊 SUCCESS METRICS

### Daily Review Metrics

1. **System Health**
   - Uptime: Target 100%
   - Component failures: Target 0
   - Restarts: Target 0

2. **Performance**
   - Query latency: Target <10ms
   - API latency: Target <100ms
   - Memory usage: Target <500MB
   - CPU usage: Target <30%

3. **Trading Activity**
   - Orders placed: (varies by strategy)
   - Fill rate: Target >80% (simulated)
   - Equity: Should remain ≈ $10,000 (paper)
   - P&L: Track but not critical (paper)

4. **Safety**
   - Circuit breaker trips: Target 0 (unless testing)
   - Validation errors: Target 0
   - Safety limit violations: Target 0

---

## ✅ POST-LAUNCH

### After 72 Hours of Stable Operation

**Review:**
- [ ] All metrics within targets
- [ ] No critical issues
- [ ] Team confident

**Decisions:**
- [ ] Continue paper trading
- [ ] Add more strategies
- [ ] Increase position sizes
- [ ] Begin planning production launch

### Before Production Launch

**Additional Requirements:**
1. [ ] 2+ weeks stable paper trading
2. [ ] All high-priority fixes complete
3. [ ] Full alerting integrated
4. [ ] Prometheus metrics exported
5. [ ] Docker deployment ready
6. [ ] Runbook complete
7. [ ] Team trained
8. [ ] Legal/compliance reviewed
9. [ ] Real API keys configured
10. [ ] paper_trading=false verified

---

## 📞 SUPPORT

**Critical Issues:**
- Check logs: `logs/trading.log`
- Check database: `sqlite3 data/trading.db`
- Check metrics: grep specific component in logs

**Questions:**
- Review documentation in code
- Check PROGRESS_REPORT.md
- Review component docstrings

**Remember:**
- This is paper trading - no real money
- All orders are simulated
- Focus on stability and reliability
- Take time to understand behavior

---

## 🎉 READY TO LAUNCH

Complete this checklist, then:

```bash
python main.py --config config/current.yaml --mode paper
```

**Good luck! 🚀**
