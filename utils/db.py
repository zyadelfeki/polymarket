from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from pathlib import Path
from config.settings import settings

Base = declarative_base()

class Trade(Base):
    __tablename__ = 'trades'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    market_id = Column(String(100))
    market_title = Column(Text)
    side = Column(String(10))
    entry_price = Column(Float)
    exit_price = Column(Float, nullable=True)
    bet_size = Column(Float)
    shares = Column(Float)
    status = Column(String(20))
    strategy = Column(String(50))
    edge = Column(Float)
    confidence = Column(Float)
    pnl = Column(Float, nullable=True)
    roi = Column(Float, nullable=True)
    exit_reason = Column(String(100), nullable=True)
    exit_timestamp = Column(DateTime, nullable=True)

class PerformanceSnapshot(Base):
    __tablename__ = 'performance'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    bankroll = Column(Float)
    open_positions = Column(Integer)
    total_trades = Column(Integer)
    winning_trades = Column(Integer)
    losing_trades = Column(Integer)
    win_rate = Column(Float)
    total_pnl = Column(Float)
    roi = Column(Float)
    sharpe_ratio = Column(Float, nullable=True)
    max_drawdown = Column(Float)

class Database:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = settings.DATABASE_PATH
        
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f'sqlite:///{db_path}')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
    
    def get_session(self):
        return self.Session()
    
    def log_trade(self, trade_data: dict):
        session = self.get_session()
        try:
            trade = Trade(**trade_data)
            session.add(trade)
            session.commit()
            return trade.id
        finally:
            session.close()
    
    def update_trade(self, trade_id: int, updates: dict):
        session = self.get_session()
        try:
            trade = session.query(Trade).filter(Trade.id == trade_id).first()
            if trade:
                for key, value in updates.items():
                    setattr(trade, key, value)
                session.commit()
        finally:
            session.close()
    
    def log_performance(self, perf_data: dict):
        session = self.get_session()
        try:
            snapshot = PerformanceSnapshot(**perf_data)
            session.add(snapshot)
            session.commit()
        finally:
            session.close()
    
    def get_open_trades(self):
        session = self.get_session()
        try:
            return session.query(Trade).filter(Trade.status == 'OPEN').all()
        finally:
            session.close()
    
    def get_performance_stats(self, days: int = 30):
        session = self.get_session()
        try:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=days)
            trades = session.query(Trade).filter(Trade.timestamp >= cutoff).all()
            
            if not trades:
                return None
            
            total = len(trades)
            wins = len([t for t in trades if t.pnl and t.pnl > 0])
            losses = len([t for t in trades if t.pnl and t.pnl < 0])
            total_pnl = sum(t.pnl for t in trades if t.pnl)
            
            return {
                'total_trades': total,
                'wins': wins,
                'losses': losses,
                'win_rate': wins / total if total > 0 else 0,
                'total_pnl': total_pnl,
                'avg_pnl_per_trade': total_pnl / total if total > 0 else 0
            }
        finally:
            session.close()