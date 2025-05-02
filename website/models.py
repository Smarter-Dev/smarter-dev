from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from .database import Base

# This is a placeholder model for future use
# We'll expand this as the website grows
class Subscriber(Base):
    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    
    def __repr__(self):
        return f"<Subscriber {self.email}>"
