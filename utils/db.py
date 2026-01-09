from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta
from pathlib import Path
from config.settings import settings

Base = declarative_base()

class Trade(Base):
    __tablename__ = 'trades'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    strategy = Column(String(50))
    market_id = Column(String(100))
    market_title = Column(Text)
    side = Column(String(10))
    entry_price = Column(Float)
    exit_price = Column(Float, nullable=True)
    amount = Column(Float)
    profit = Column(Float, nullable=True)
    roi = Column(Float, nullable=True)
    confidence = Column(Float)
    edge = Column(Float)
    status = Column(String(20))
    exit_timestamp = Column(DateTime, nullable=True)
    exit_reason = Column(String(100), nullable=True)

class VolatilityEvent(Base):
    __tablename__ = 'volatility_events'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    symbol = Column(String(10))
    volatility_pct = Column(Float)
    price = Column(Float)
    direction = Column(String(10))
    opportunities_found = Column(Integer)
    trades_executed = Column(Integer)

class WhaleActivity(Base):
    __tablename__ = 'whale_activity'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    wallet_address = Column(String(100))
    market_id = Column(String(100))
    side = Column(String(10))
    amount = Column(Float)
    copied = Column(Boolean, default=False)
    copy_amount = Column(Float, nullable=True)

class PerformanceMetric(Base):
    __tablename__ = 'performance_metrics'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    capital = Column(Float)
    open_positions = Column(Integer)
    daily_pnl = Column(Float)
    total_pnl = Column(Float)
    win_rate = Column(Float)
    sharpe_ratio = Column(Float, nullable=True)
    max_drawdown = Column(Float)

class Database:
    def __init__(self):
        db_path = Path(settings.DATABASE_PATH)
        db_path.parent.mkdir(exist_ok=True)
        
        self.engine = create_engine(f'sqlite:///{db_path}', echo=False)
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
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def update_trade(self, trade_id: int, updates: dict):
        session = self.get_session()
        try:
            trade = session.query(Trade).filter_by(id=trade_id).first()
            if trade:
                for key, value in updates.items():
                    setattr(trade, key, value)
                session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def log_volatility_event(self, event_data: dict):
        session = self.get_session()
        try:
            event = VolatilityEvent(**event_data)
            session.add(event)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def log_whale_activity(self, whale_data: dict):
        session = self.get_session()
        try:
            activity = WhaleActivity(**whale_data)
            session.add(activity)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def log_performance(self, metrics: dict):
        session = self.get_session()
        try:
            perf = PerformanceMetric(**metrics)
            session.add(perf)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def get_open_trades(self):
        session = self.get_session()
        try:
            return session.query(Trade).filter_by(status='OPEN').all()
        finally:
            session.close()
    
    def get_performance_history(self, days: int = 7):
        session = self.get_session()
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            return session.query(PerformanceMetric).filter(
                PerformanceMetric.timestamp >= cutoff
            ).all()
        finally:
            session.close()

db = Database()