from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, func
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base

# This is a placeholder model for future use
class Subscriber(Base):
    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<Subscriber {self.email}>"

# Admin user model
class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<AdminUser {self.username}>"

# Redirect model
class Redirect(Base):
    __tablename__ = "redirects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)  # URL-safe name for the redirect
    target_url = Column(String)  # URL to redirect to
    description = Column(Text, nullable=True)  # Optional description
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationship with clicks
    clicks = relationship("RedirectClick", back_populates="redirect", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Redirect {self.name} -> {self.target_url}>"

# Redirect click tracking model
class RedirectClick(Base):
    __tablename__ = "redirect_clicks"

    id = Column(Integer, primary_key=True, index=True)
    redirect_id = Column(Integer, ForeignKey("redirects.id"))
    ip_address = Column(String, nullable=True)  # Store IP address (consider privacy implications)
    user_agent = Column(String, nullable=True)  # Store user agent
    referer = Column(String, nullable=True)  # Store referer URL
    timestamp = Column(DateTime, default=func.now())

    # Relationship with redirect
    redirect = relationship("Redirect", back_populates="clicks")

    def __repr__(self):
        return f"<RedirectClick for {self.redirect_id} at {self.timestamp}>"
