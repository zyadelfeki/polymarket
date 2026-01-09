from typing import Dict, List, Optional
from datetime import datetime
import logging
from decimal import Decimal
from config.settings import settings

logger = logging.getLogger(__name__)

class Position:
    def __init__(
        self,
        position_id: str,
        market_id: str,
        market_title: str,
        strategy: str,
        side: str,
        entry_price: float,
        amount: Decimal,
        confidence: float,
        edge: float
    ):
        self.position_id = position_id
        self.market_id = market_id
        self.market_title = market_title
        self.strategy = strategy
        self.side = side
        self.entry_price = entry_price
        self.amount = amount
        self.confidence = confidence
        self.edge = edge
        self.entry_time = datetime.utcnow()
        self.current_price = entry_price
        self.unrealized_pnl = Decimal("0")
        self.status = "OPEN"
    
    def update_price(self, current_price: float):
        self.current_price = current_price
        price_change = current_price - self.entry_price
        self.unrealized_pnl = Decimal(str(price_change)) * self.amount / Decimal(str(self.entry_price))
    
    def get_hold_time_minutes(self) -> float:
        return (datetime.utcnow() - self.entry_time).total_seconds() / 60
    
    def to_dict(self) -> Dict:
        return {
            "position_id": self.position_id,
            "market_id": self.market_id,
            "market_title": self.market_title,
            "strategy": self.strategy,
            "side": self.side,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "amount": float(self.amount),
            "unrealized_pnl": float(self.unrealized_pnl),
            "confidence": self.confidence,
            "edge": self.edge,
            "hold_time_minutes": self.get_hold_time_minutes(),
            "status": self.status
        }

class PositionManager:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.closed_positions: List[Position] = []
        self.total_capital_deployed = Decimal("0")
    
    def can_open_position(self, required_capital: Decimal) -> bool:
        if len(self.positions) >= settings.MAX_OPEN_POSITIONS:
            logger.warning(f"Max positions reached: {settings.MAX_OPEN_POSITIONS}")
            return False
        
        if required_capital < settings.MIN_BET_SIZE:
            logger.warning(f"Position size too small: ${required_capital}")
            return False
        
        return True
    
    def open_position(
        self,
        position_id: str,
        market_id: str,
        market_title: str,
        strategy: str,
        side: str,
        entry_price: float,
        amount: Decimal,
        confidence: float,
        edge: float
    ) -> Optional[Position]:
        
        if not self.can_open_position(amount):
            return None
        
        position = Position(
            position_id=position_id,
            market_id=market_id,
            market_title=market_title,
            strategy=strategy,
            side=side,
            entry_price=entry_price,
            amount=amount,
            confidence=confidence,
            edge=edge
        )
        
        self.positions[position_id] = position
        self.total_capital_deployed += amount
        
        logger.info(f"Position opened: {position_id} | {strategy} | ${amount:.2f} @ ${entry_price:.3f}")
        return position
    
    def close_position(self, position_id: str, exit_price: float, exit_reason: str) -> Optional[Dict]:
        if position_id not in self.positions:
            logger.error(f"Position not found: {position_id}")
            return None
        
        position = self.positions[position_id]
        position.status = "CLOSED"
        
        shares = position.amount / Decimal(str(position.entry_price))
        realized_pnl = shares * Decimal(str(exit_price - position.entry_price))
        roi = (realized_pnl / position.amount) * Decimal("100")
        
        result = {
            "position_id": position_id,
            "market_title": position.market_title,
            "strategy": position.strategy,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "amount": float(position.amount),
            "realized_pnl": float(realized_pnl),
            "roi": float(roi),
            "hold_time_minutes": position.get_hold_time_minutes(),
            "exit_reason": exit_reason
        }
        
        self.closed_positions.append(position)
        del self.positions[position_id]
        self.total_capital_deployed -= position.amount
        
        logger.info(f"Position closed: {position_id} | P&L: ${realized_pnl:+.2f} ({roi:+.1f}%) | {exit_reason}")
        return result
    
    def update_position_prices(self, price_updates: Dict[str, float]):
        for position_id, position in self.positions.items():
            if position.market_id in price_updates:
                position.update_price(price_updates[position.market_id])
    
    def get_open_positions(self) -> List[Position]:
        return list(self.positions.values())
    
    def get_position_count(self) -> int:
        return len(self.positions)
    
    def get_total_exposure(self) -> Decimal:
        return self.total_capital_deployed
    
    def get_unrealized_pnl(self) -> Decimal:
        return sum(pos.unrealized_pnl for pos in self.positions.values())
    
    def get_statistics(self) -> Dict:
        open_positions = len(self.positions)
        total_closed = len(self.closed_positions)
        
        if total_closed > 0:
            winning_trades = sum(1 for p in self.closed_positions if p.unrealized_pnl > 0)
            win_rate = winning_trades / total_closed
        else:
            win_rate = 0.0
        
        return {
            "open_positions": open_positions,
            "total_exposure": float(self.total_capital_deployed),
            "unrealized_pnl": float(self.get_unrealized_pnl()),
            "closed_trades": total_closed,
            "win_rate": win_rate
        }