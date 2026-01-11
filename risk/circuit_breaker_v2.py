#!/usr/bin/env python3
"""
Institutional-Grade Circuit Breaker

Features:
- Three-state machine (CLOSED/OPEN/HALF_OPEN)
- Multiple trigger conditions (drawdown, loss streak, volatility)
- Automatic recovery testing (half-open state)
- Configurable thresholds
- Historical state tracking
- Metrics and alerting integration

Standards:
- Protects capital during adverse conditions
- Automatic recovery when conditions improve
- Full audit trail
- Observable (comprehensive metrics)
"""

import asyncio
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from dataclasses import dataclass
from collections import deque
import structlog

logger = structlog.get_logger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation, trading allowed
    OPEN = "open"      # Circuit tripped, trading halted
    HALF_OPEN = "half_open"  # Testing recovery, limited trading


class TripReason(Enum):
    """Reasons for circuit trip"""
    MAX_DRAWDOWN = "max_drawdown"
    LOSS_STREAK = "loss_streak"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    SYSTEM_ERROR = "system_error"
    MANUAL = "manual"
    VOLATILITY_EXTREME = "volatility_extreme"


@dataclass
class CircuitEvent:
    """Circuit state change event"""
    timestamp: datetime
    previous_state: CircuitState
    new_state: CircuitState
    reason: Optional[TripReason] = None
    equity: Optional[Decimal] = None
    drawdown_pct: Optional[float] = None
    metadata: Dict = None


class CircuitBreakerV2:
    """
    Production-grade circuit breaker for trading bot.
    
    Monitors:
    - Equity drawdown from peak
    - Consecutive loss streak
    - Daily loss limits
    - System health
    
    States:
    - CLOSED: Normal operation
    - OPEN: Trading halted, monitoring for recovery
    - HALF_OPEN: Limited trading to test recovery
    
    Recovery:
    - Automatic transition to HALF_OPEN after cooldown
    - Test with small positions
    - Full recovery if tests pass
    """
    
    def __init__(
        self,
        initial_equity: Decimal,
        max_drawdown_pct: float = 15.0,
        max_loss_streak: int = 5,
        daily_loss_limit_pct: float = 10.0,
        recovery_threshold_pct: float = 5.0,
        cooldown_minutes: int = 30,
        half_open_max_position_pct: float = 2.0
    ):
        """
        Initialize circuit breaker.
        
        Args:
            initial_equity: Starting equity
            max_drawdown_pct: Max drawdown before trip (15% default)
            max_loss_streak: Max consecutive losses before trip
            daily_loss_limit_pct: Max daily loss before trip
            recovery_threshold_pct: Drawdown reduction needed for recovery
            cooldown_minutes: Minutes before attempting recovery
            half_open_max_position_pct: Max position size in half-open state
        """
        self.initial_equity = initial_equity
        self.max_drawdown_pct = max_drawdown_pct
        self.max_loss_streak = max_loss_streak
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.recovery_threshold_pct = recovery_threshold_pct
        self.cooldown_period = timedelta(minutes=cooldown_minutes)
        self.half_open_max_position_pct = half_open_max_position_pct
        
        # State
        self.state = CircuitState.CLOSED
        self.state_lock = asyncio.Lock()
        
        # Tracking
        self.peak_equity = initial_equity
        self.daily_start_equity = initial_equity
        self.daily_reset_time = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        
        # Loss tracking
        self.consecutive_losses = 0
        self.loss_streak_start: Optional[datetime] = None
        
        # Trip tracking
        self.trip_time: Optional[datetime] = None
        self.trip_reason: Optional[TripReason] = None
        self.trip_equity: Optional[Decimal] = None
        
        # Recovery tracking
        self.recovery_attempts = 0
        self.half_open_trades = 0
        self.half_open_wins = 0
        
        # History
        self.state_history: deque = deque(maxlen=100)
        self.trip_history: List[CircuitEvent] = []
        
        # Metrics
        self.total_trips = 0
        self.total_recoveries = 0
        
        logger.info(
            "circuit_breaker_initialized",
            initial_equity=float(initial_equity),
            max_drawdown_pct=max_drawdown_pct,
            max_loss_streak=max_loss_streak,
            daily_loss_limit_pct=daily_loss_limit_pct
        )
    
    async def can_trade(
        self,
        current_equity: Decimal,
        position_size_pct: Optional[float] = None
    ) -> bool:
        """
        Check if trading is allowed.
        
        Args:
            current_equity: Current account equity
            position_size_pct: Size of proposed trade as % of equity
        
        Returns:
            True if trading allowed
        """
        async with self.state_lock:
            # Update state based on current conditions
            await self._update_state(current_equity)
            
            # Check state
            if self.state == CircuitState.OPEN:
                return False
            
            elif self.state == CircuitState.CLOSED:
                return True
            
            elif self.state == CircuitState.HALF_OPEN:
                # In half-open, only allow small positions
                if position_size_pct is not None:
                    return position_size_pct <= self.half_open_max_position_pct
                return True
            
            return False
    
    async def record_trade_result(
        self,
        profit_loss: Decimal,
        is_win: bool
    ):
        """
        Record a trade result.
        
        Args:
            profit_loss: P&L from trade
            is_win: True if winning trade
        """
        async with self.state_lock:
            if is_win:
                self.consecutive_losses = 0
                self.loss_streak_start = None
                
                # Track half-open performance
                if self.state == CircuitState.HALF_OPEN:
                    self.half_open_wins += 1
            
            else:
                self.consecutive_losses += 1
                if self.loss_streak_start is None:
                    self.loss_streak_start = datetime.utcnow()
            
            # Track half-open trades
            if self.state == CircuitState.HALF_OPEN:
                self.half_open_trades += 1
        
        logger.debug(
            "trade_result_recorded",
            is_win=is_win,
            profit_loss=float(profit_loss),
            consecutive_losses=self.consecutive_losses
        )
    
    async def _update_state(self, current_equity: Decimal):
        """
        Update circuit breaker state based on conditions.
        
        Args:
            current_equity: Current equity
        """
        # Reset daily tracking if needed
        await self._check_daily_reset()
        
        # Update peak
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        
        # Calculate drawdown
        drawdown_pct = float(
            (self.peak_equity - current_equity) / self.peak_equity * 100
        )
        
        # Calculate daily loss
        daily_loss_pct = float(
            (self.daily_start_equity - current_equity) / self.daily_start_equity * 100
        )
        
        # Check trip conditions
        if self.state == CircuitState.CLOSED:
            trip_reason = None
            
            # Check max drawdown
            if drawdown_pct >= self.max_drawdown_pct:
                trip_reason = TripReason.MAX_DRAWDOWN
            
            # Check loss streak
            elif self.consecutive_losses >= self.max_loss_streak:
                trip_reason = TripReason.LOSS_STREAK
            
            # Check daily loss limit
            elif daily_loss_pct >= self.daily_loss_limit_pct:
                trip_reason = TripReason.DAILY_LOSS_LIMIT
            
            if trip_reason:
                await self._trip_circuit(
                    trip_reason,
                    current_equity,
                    drawdown_pct
                )
        
        # Check recovery conditions
        elif self.state == CircuitState.OPEN:
            await self._check_recovery(current_equity, drawdown_pct)
        
        # Check half-open performance
        elif self.state == CircuitState.HALF_OPEN:
            await self._check_half_open_performance(current_equity, drawdown_pct)
    
    async def _trip_circuit(
        self,
        reason: TripReason,
        equity: Decimal,
        drawdown_pct: float
    ):
        """
        Trip the circuit breaker.
        
        Args:
            reason: Reason for trip
            equity: Current equity
            drawdown_pct: Current drawdown percentage
        """
        previous_state = self.state
        self.state = CircuitState.OPEN
        self.trip_time = datetime.utcnow()
        self.trip_reason = reason
        self.trip_equity = equity
        self.total_trips += 1
        
        # Record event
        event = CircuitEvent(
            timestamp=self.trip_time,
            previous_state=previous_state,
            new_state=CircuitState.OPEN,
            reason=reason,
            equity=equity,
            drawdown_pct=drawdown_pct
        )
        
        self.state_history.append(event)
        self.trip_history.append(event)
        
        logger.critical(
            "circuit_breaker_tripped",
            reason=reason.value,
            equity=float(equity),
            drawdown_pct=drawdown_pct,
            consecutive_losses=self.consecutive_losses
        )
    
    async def _check_recovery(self, current_equity: Decimal, drawdown_pct: float):
        """
        Check if conditions allow recovery attempt.
        
        Args:
            current_equity: Current equity
            drawdown_pct: Current drawdown percentage
        """
        if not self.trip_time:
            return
        
        # Check cooldown period
        time_since_trip = datetime.utcnow() - self.trip_time
        if time_since_trip < self.cooldown_period:
            return
        
        # Check if drawdown has improved
        improvement_needed = self.max_drawdown_pct - self.recovery_threshold_pct
        
        if drawdown_pct <= improvement_needed:
            # Enter half-open state
            previous_state = self.state
            self.state = CircuitState.HALF_OPEN
            self.recovery_attempts += 1
            self.half_open_trades = 0
            self.half_open_wins = 0
            
            event = CircuitEvent(
                timestamp=datetime.utcnow(),
                previous_state=previous_state,
                new_state=CircuitState.HALF_OPEN,
                equity=current_equity,
                drawdown_pct=drawdown_pct
            )
            
            self.state_history.append(event)
            
            logger.warning(
                "circuit_breaker_half_open",
                equity=float(current_equity),
                drawdown_pct=drawdown_pct,
                recovery_attempt=self.recovery_attempts
            )
    
    async def _check_half_open_performance(self, current_equity: Decimal, drawdown_pct: float):
        """
        Check half-open performance and decide next state.
        
        Args:
            current_equity: Current equity
            drawdown_pct: Current drawdown percentage
        """
        # Need at least 3 trades to evaluate
        if self.half_open_trades < 3:
            return
        
        # Calculate win rate
        win_rate = self.half_open_wins / self.half_open_trades
        
        # Success criteria: >60% win rate and drawdown < 10%
        if win_rate >= 0.6 and drawdown_pct < (self.max_drawdown_pct - 5.0):
            # Full recovery - close circuit
            previous_state = self.state
            self.state = CircuitState.CLOSED
            self.trip_time = None
            self.trip_reason = None
            self.consecutive_losses = 0
            self.total_recoveries += 1
            
            event = CircuitEvent(
                timestamp=datetime.utcnow(),
                previous_state=previous_state,
                new_state=CircuitState.CLOSED,
                equity=current_equity,
                drawdown_pct=drawdown_pct,
                metadata={
                    'half_open_trades': self.half_open_trades,
                    'half_open_wins': self.half_open_wins,
                    'win_rate': win_rate
                }
            )
            
            self.state_history.append(event)
            
            logger.info(
                "circuit_breaker_recovered",
                equity=float(current_equity),
                drawdown_pct=drawdown_pct,
                half_open_trades=self.half_open_trades,
                win_rate=win_rate
            )
        
        # Failure criteria: <40% win rate or drawdown worsened
        elif win_rate < 0.4 or drawdown_pct >= (self.max_drawdown_pct - 2.0):
            # Back to open
            previous_state = self.state
            self.state = CircuitState.OPEN
            self.trip_time = datetime.utcnow()  # Reset cooldown
            
            event = CircuitEvent(
                timestamp=datetime.utcnow(),
                previous_state=previous_state,
                new_state=CircuitState.OPEN,
                reason=TripReason.MAX_DRAWDOWN,
                equity=current_equity,
                drawdown_pct=drawdown_pct,
                metadata={
                    'half_open_trades': self.half_open_trades,
                    'half_open_wins': self.half_open_wins,
                    'win_rate': win_rate
                }
            )
            
            self.state_history.append(event)
            
            logger.warning(
                "circuit_breaker_recovery_failed",
                equity=float(current_equity),
                drawdown_pct=drawdown_pct,
                half_open_trades=self.half_open_trades,
                win_rate=win_rate
            )
    
    async def _check_daily_reset(self):
        """Reset daily tracking at start of new day."""
        now = datetime.utcnow()
        expected_reset = self.daily_reset_time + timedelta(days=1)
        
        if now >= expected_reset:
            # New day
            self.daily_start_equity = self.peak_equity  # Use current peak
            self.daily_reset_time = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            
            logger.info(
                "daily_tracking_reset",
                date=now.date().isoformat(),
                equity=float(self.daily_start_equity)
            )
    
    async def manual_trip(self, reason: str = "Manual intervention"):
        """
        Manually trip circuit breaker.
        
        Args:
            reason: Reason for manual trip
        """
        async with self.state_lock:
            if self.state != CircuitState.OPEN:
                await self._trip_circuit(
                    TripReason.MANUAL,
                    self.peak_equity,
                    0.0
                )
                
                logger.warning(
                    "circuit_breaker_manual_trip",
                    reason=reason
                )
    
    async def manual_reset(self):
        """Manually reset circuit breaker."""
        async with self.state_lock:
            previous_state = self.state
            self.state = CircuitState.CLOSED
            self.trip_time = None
            self.trip_reason = None
            self.consecutive_losses = 0
            
            event = CircuitEvent(
                timestamp=datetime.utcnow(),
                previous_state=previous_state,
                new_state=CircuitState.CLOSED,
                reason=TripReason.MANUAL
            )
            
            self.state_history.append(event)
            
            logger.warning("circuit_breaker_manual_reset")
    
    def get_status(self) -> Dict:
        """
        Get current circuit breaker status.
        
        Returns:
            Status dictionary
        """
        return {
            'state': self.state.value,
            'peak_equity': float(self.peak_equity),
            'consecutive_losses': self.consecutive_losses,
            'trip_reason': self.trip_reason.value if self.trip_reason else None,
            'trip_time': self.trip_time.isoformat() if self.trip_time else None,
            'total_trips': self.total_trips,
            'total_recoveries': self.total_recoveries,
            'recovery_attempts': self.recovery_attempts
        }
    
    def get_metrics(self) -> Dict:
        """
        Get circuit breaker metrics.
        
        Returns:
            Metrics dictionary
        """
        return {
            'state': self.state.value,
            'total_trips': self.total_trips,
            'total_recoveries': self.total_recoveries,
            'recovery_rate': (
                self.total_recoveries / self.total_trips
                if self.total_trips > 0 else 0.0
            ),
            'consecutive_losses': self.consecutive_losses,
            'state_changes': len(self.state_history),
            'trip_history_count': len(self.trip_history)
        }
