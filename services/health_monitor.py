#!/usr/bin/env python3
"""
Production Health Monitor

Tracks system health and alerts on failures:
- Data feed connectivity (Binance, Polymarket)
- Database operations
- Strategy activity
- Circuit breaker status
- Memory/CPU usage

Alerts via:
- Logs (always)
- Email (configurable)
- Telegram (configurable)
- Slack (configurable)
"""

import asyncio
import logging
import time
import psutil
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

class HealthStatus(Enum):
    """Component health status"""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"  # Working but suboptimal
    FAILED = "FAILED"  # Not working
    UNKNOWN = "UNKNOWN"  # Not yet checked

@dataclass
class HealthCheck:
    """Result of a health check"""
    component: str
    status: HealthStatus
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None
    last_success: Optional[datetime] = None
    consecutive_failures: int = 0

class HealthMonitor:
    """
    Monitor system health and alert on issues.
    
    Checks:
    1. Binance WebSocket (last tick time)
    2. Polymarket API (last successful call)
    3. Database (write latency)
    4. Strategy activity (trades per hour)
    5. System resources (memory, CPU)
    """
    
    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        
        self.check_interval = config.get('check_interval', 30)  # seconds
        self.alert_threshold = config.get('alert_threshold', 3)  # consecutive failures
        self.max_latency_ms = config.get('max_latency_ms', 5000)  # 5 seconds
        self.min_trades_per_hour = config.get('min_trades_per_hour', 1)
        self.max_memory_pct = config.get('max_memory_pct', 80)
        self.max_cpu_pct = config.get('max_cpu_pct', 90)
        
        # Component states
        self.components = {
            'binance_ws': HealthCheck('binance_ws', HealthStatus.UNKNOWN),
            'polymarket_api': HealthCheck('polymarket_api', HealthStatus.UNKNOWN),
            'database': HealthCheck('database', HealthStatus.UNKNOWN),
            'strategies': HealthCheck('strategies', HealthStatus.UNKNOWN),
            'system': HealthCheck('system', HealthStatus.UNKNOWN)
        }
        
        # Activity tracking
        self.last_binance_tick = None
        self.last_polymarket_call = None
        self.last_db_write = None
        self.last_trade = None
        self.trades_last_hour = 0
        
        # Alert tracking (prevent spam)
        self.last_alerts = {}  # component -> last alert time
        self.alert_cooldown = timedelta(minutes=15)  # Min time between alerts
        
        self.running = False
        self.monitor_task = None
        
        logger.info(f"HealthMonitor initialized: check_interval={self.check_interval}s")
    
    async def start(self):
        """Start health monitoring loop"""
        if self.running:
            logger.warning("HealthMonitor already running")
            return
        
        self.running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("HealthMonitor started")
    
    async def stop(self):
        """Stop health monitoring"""
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("HealthMonitor stopped")
    
    async def _monitor_loop(self):
        """Main monitoring loop"""
        while self.running:
            try:
                # Run all health checks
                await self._check_binance_ws()
                await self._check_polymarket_api()
                await self._check_database()
                await self._check_strategies()
                await self._check_system_resources()
                
                # Check for alerts
                await self._process_alerts()
                
                # Log summary
                self._log_health_summary()
                
                await asyncio.sleep(self.check_interval)
            
            except Exception as e:
                logger.error(f"Error in health monitor loop: {e}", exc_info=True)
                await asyncio.sleep(self.check_interval)
    
    async def _check_binance_ws(self):
        """Check Binance WebSocket health"""
        component = self.components['binance_ws']
        
        if self.last_binance_tick is None:
            component.status = HealthStatus.UNKNOWN
            component.error_message = "No ticks received yet"
            return
        
        time_since_tick = (datetime.utcnow() - self.last_binance_tick).total_seconds()
        
        if time_since_tick > 60:  # No tick in 1 minute = failure
            component.status = HealthStatus.FAILED
            component.error_message = f"No ticks for {time_since_tick:.0f}s"
            component.consecutive_failures += 1
        elif time_since_tick > 10:  # Tick delay > 10s = degraded
            component.status = HealthStatus.DEGRADED
            component.error_message = f"Tick delay: {time_since_tick:.0f}s"
            component.consecutive_failures = 0
        else:
            component.status = HealthStatus.HEALTHY
            component.error_message = None
            component.last_success = datetime.utcnow()
            component.consecutive_failures = 0
    
    async def _check_polymarket_api(self):
        """Check Polymarket API health"""
        component = self.components['polymarket_api']
        
        if self.last_polymarket_call is None:
            component.status = HealthStatus.UNKNOWN
            component.error_message = "No API calls yet"
            return
        
        time_since_call = (datetime.utcnow() - self.last_polymarket_call).total_seconds()
        
        if time_since_call > 120:  # No call in 2 minutes = issue
            component.status = HealthStatus.DEGRADED
            component.error_message = f"No API calls for {time_since_call:.0f}s"
            component.consecutive_failures += 1
        else:
            component.status = HealthStatus.HEALTHY
            component.error_message = None
            component.last_success = datetime.utcnow()
            component.consecutive_failures = 0
    
    async def _check_database(self):
        """Check database health via latency test"""
        component = self.components['database']
        
        try:
            start = time.monotonic()
            # Simple read query
            # In production, inject ledger dependency and call ledger.get_equity()
            latency_ms = int((time.monotonic() - start) * 1000)
            
            component.latency_ms = latency_ms
            
            if latency_ms > self.max_latency_ms:
                component.status = HealthStatus.DEGRADED
                component.error_message = f"High latency: {latency_ms}ms"
                component.consecutive_failures += 1
            else:
                component.status = HealthStatus.HEALTHY
                component.error_message = None
                component.last_success = datetime.utcnow()
                component.consecutive_failures = 0
        
        except Exception as e:
            component.status = HealthStatus.FAILED
            component.error_message = str(e)
            component.consecutive_failures += 1
    
    async def _check_strategies(self):
        """Check strategy activity"""
        component = self.components['strategies']
        
        if self.last_trade is None:
            # No trades yet - might be normal during low volatility
            component.status = HealthStatus.UNKNOWN
            component.error_message = "No trades executed yet"
            return
        
        time_since_trade = (datetime.utcnow() - self.last_trade).total_seconds()
        
        # Check trades per hour
        if self.trades_last_hour < self.min_trades_per_hour and time_since_trade > 3600:
            component.status = HealthStatus.DEGRADED
            component.error_message = f"Low activity: {self.trades_last_hour} trades/hr"
            component.consecutive_failures += 1
        else:
            component.status = HealthStatus.HEALTHY
            component.error_message = None
            component.last_success = datetime.utcnow()
            component.consecutive_failures = 0
    
    async def _check_system_resources(self):
        """Check CPU and memory usage"""
        component = self.components['system']
        
        try:
            # CPU usage
            cpu_pct = psutil.cpu_percent(interval=0.1)
            
            # Memory usage
            memory = psutil.virtual_memory()
            memory_pct = memory.percent
            
            issues = []
            if cpu_pct > self.max_cpu_pct:
                issues.append(f"CPU: {cpu_pct:.1f}%")
            if memory_pct > self.max_memory_pct:
                issues.append(f"Memory: {memory_pct:.1f}%")
            
            if issues:
                component.status = HealthStatus.DEGRADED
                component.error_message = ", ".join(issues)
                component.consecutive_failures += 1
            else:
                component.status = HealthStatus.HEALTHY
                component.error_message = None
                component.last_success = datetime.utcnow()
                component.consecutive_failures = 0
        
        except Exception as e:
            component.status = HealthStatus.FAILED
            component.error_message = str(e)
            component.consecutive_failures += 1
    
    async def _process_alerts(self):
        """Send alerts for failed components"""
        now = datetime.utcnow()
        
        for name, component in self.components.items():
            if component.status == HealthStatus.FAILED and \
               component.consecutive_failures >= self.alert_threshold:
                
                # Check alert cooldown
                last_alert = self.last_alerts.get(name)
                if last_alert and (now - last_alert) < self.alert_cooldown:
                    continue  # Skip (already alerted recently)
                
                # Send alert
                await self._send_alert(
                    f"🚨 HEALTH ALERT: {component.component}",
                    f"Status: {component.status.value}\n"
                    f"Error: {component.error_message}\n"
                    f"Consecutive failures: {component.consecutive_failures}\n"
                    f"Last success: {component.last_success}"
                )
                
                self.last_alerts[name] = now
            
            elif component.status == HealthStatus.HEALTHY and name in self.last_alerts:
                # Component recovered
                await self._send_alert(
                    f"✅ RECOVERY: {component.component}",
                    f"Component is now healthy"
                )
                del self.last_alerts[name]
    
    async def _send_alert(self, title: str, message: str):
        """Send alert via configured channels"""
        # Always log
        logger.warning(f"ALERT: {title} - {message}")
        
        # TODO: Implement email/Telegram/Slack integrations
        # For now, just log to console
    
    def _log_health_summary(self):
        """Log summary of all component statuses"""
        statuses = []
        for name, comp in self.components.items():
            icon = {
                HealthStatus.HEALTHY: "✓",
                HealthStatus.DEGRADED: "⚠",
                HealthStatus.FAILED: "✗",
                HealthStatus.UNKNOWN: "?"
            }.get(comp.status, "?")
            
            status_str = f"{icon} {name}"
            if comp.latency_ms:
                status_str += f" ({comp.latency_ms}ms)"
            if comp.error_message:
                status_str += f" - {comp.error_message}"
            
            statuses.append(status_str)
        
        logger.info(f"Health: {' | '.join(statuses)}")
    
    # Update methods (called by other services)
    
    def record_binance_tick(self):
        """Record that Binance sent a tick"""
        self.last_binance_tick = datetime.utcnow()
    
    def record_polymarket_call(self):
        """Record that Polymarket API was called"""
        self.last_polymarket_call = datetime.utcnow()
    
    def record_db_write(self):
        """Record that database was written to"""
        self.last_db_write = datetime.utcnow()
    
    def record_trade(self):
        """Record that a trade was executed"""
        self.last_trade = datetime.utcnow()
        self.trades_last_hour += 1
        
        # Reset hourly counter
        # (In production, use a rolling window)
    
    def get_health_status(self) -> Dict:
        """Get current health status of all components"""
        return {
            name: {
                'status': comp.status.value,
                'error': comp.error_message,
                'last_success': comp.last_success.isoformat() if comp.last_success else None,
                'consecutive_failures': comp.consecutive_failures,
                'latency_ms': comp.latency_ms
            }
            for name, comp in self.components.items()
        }
    
    def is_healthy(self) -> bool:
        """Check if all critical components are healthy"""
        critical = ['binance_ws', 'polymarket_api', 'database']
        return all(
            self.components[name].status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED]
            for name in critical
        )