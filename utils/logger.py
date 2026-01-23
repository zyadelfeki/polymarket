import logging
import sys
from datetime import datetime
from pathlib import Path
from config.settings import settings
from utils.correlation_id import CorrelationIdFilter

class PerformanceLogger:
    def __init__(self):
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.total_profit = 0.0
        self.start_time = datetime.utcnow()
    
    def log_trade(self, profit: float, win: bool):
        self.trade_count += 1
        if win:
            self.win_count += 1
        else:
            self.loss_count += 1
        self.total_profit += profit
    
    def get_stats(self):
        win_rate = (self.win_count / self.trade_count * 100) if self.trade_count > 0 else 0
        avg_profit = self.total_profit / self.trade_count if self.trade_count > 0 else 0
        return {
            "trades": self.trade_count,
            "wins": self.win_count,
            "losses": self.loss_count,
            "win_rate": win_rate,
            "total_profit": self.total_profit,
            "avg_profit": avg_profit,
            "runtime_hours": (datetime.utcnow() - self.start_time).total_seconds() / 3600
        }

def setup_logger():
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    log_file = log_dir / f"bot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log"
    
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL),
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(correlation_id)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )

    root_logger = logging.getLogger()
    root_logger.addFilter(CorrelationIdFilter())
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized: {log_file}")
    return logger

performance_logger = PerformanceLogger()