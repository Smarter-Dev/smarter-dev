import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Use absolute path for the database
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), 'smarter_dev.db'))
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# Configure engine with optimized pool settings
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=QueuePool,
    pool_size=20,  # Increased from 5
    max_overflow=20,  # Increased from 10
    pool_timeout=60,  # Increased from 30
    pool_recycle=300,  # Reduced from 1800 to recycle connections more frequently
    pool_pre_ping=True,  # Keep this to check connection validity
    echo=False  # Set to True for debugging SQL queries
)

# Add event listeners for connection pool monitoring
@event.listens_for(engine, "checkout")
def checkout_connection(dbapi_connection, connection_record, connection_proxy):
    connection_record.info.setdefault('checkout_time', time.time())

@event.listens_for(engine, "checkin")
def checkin_connection(dbapi_connection, connection_record):
    checkout_time = connection_record.info.get('checkout_time')
    if checkout_time is not None:
        connection_record.info.pop('checkout_time')
        total_time = time.time() - checkout_time
        if total_time > 3:  # Reduced from 5 to catch slower connections earlier
            logger.warning(f"Connection held for {total_time:.2f} seconds")

@event.listens_for(engine, "connect")
def connect(dbapi_connection, connection_record):
    logger.debug("New database connection established")

@event.listens_for(engine, "reset")
def reset_connection(dbapi_connection, connection_record):
    logger.debug("Connection reset")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Dependency to get DB session with timeout
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Function to dispose of all connections in the pool
def dispose_engine_connections():
    """
    Dispose of all connections in the engine's connection pool.
    Call this function when you need to forcibly clean up all connections,
    such as during application shutdown or when experiencing connection issues.
    """
    logger.info("Disposing all connections in the pool")
    engine.dispose()
    logger.info("All connections have been disposed")
