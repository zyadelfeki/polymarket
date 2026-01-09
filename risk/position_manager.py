"""
Position Manager
Tracks open positions and exposure limits
"""
from typing import List, Dict, Optional
from decimal import Decimal
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class PositionManager:
    """Real-time position tracking"""
    
    def __init__(self, max_positions: int = 3):
        self.max_positions = max_positions
        self.open_positions: List[Dict] = []
    
    def can_open_position(self) -> bool:
        """Check if we can open new position"""
        return len(self.open_positions) < self.max_positions
    
    def add_position(self, market_id: str, side: str, entry_price: float, 
                    size: Decimal, confidence: float, reason: str) -> str:
        """Add new position"""
        position_id = f"{market_id}_{side}_{datetime.utcnow().timestamp()}"
        
        position = {
            "id": position_id,
            "market_id": market_id,
            "side": side,
            "entry_price": entry_price,
            "size": size,
            "confidence": confidence,
            "reason": reason,
            "entry_time": datetime.utcnow(),
            "status": "OPEN"
        }
        
        self.open_positions.append(position)
        logger.info(f"➕ Position opened: {side} @ ${entry_price:.3f} | {reason}")
        
        return position_id
    
    def close_position(self, position_id: str, exit_price: float, reason: str) -> Optional[Dict]:
        """Close position and calculate P&L"""
        for i, pos in enumerate(self.open_positions):
            if pos["id"] == position_id:
                pos["exit_price"] = exit_price
                pos["exit_time"] = datetime.utcnow()
                pos["exit_reason"] = reason
                pos["status"] = "CLOSED"
                
                # Calculate P&L
                shares = float(pos["size"]) / pos["entry_price"]
                exit_value = shares * exit_price
                profit = exit_value - float(pos["size"])
                roi = (profit / float(pos["size"])) * 100
                
                pos["profit"] = Decimal(str(profit))
                pos["roi"] = roi
                
                closed = self.open_positions.pop(i)
                
                logger.info(f"✖️ Position closed: {reason}")
                logger.info(f"   P&L: ${profit:+.2f} ({roi:+.1f}%)")
                
                return closed
        
        return None
    
    def get_total_exposure(self) -> Decimal:
        """Calculate total capital in open positions"""
        return sum(pos["size"] for pos in self.open_positions)
    
    def get_positions(self) -> List[Dict]:
        return self.open_positions
    
    def get_stats(self) -> Dict:
        return {
            "open_count": len(self.open_positions),
            "max_positions": self.max_positions,
            "total_exposure": float(self.get_total_exposure())
        }