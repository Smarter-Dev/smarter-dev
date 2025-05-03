from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float, func
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

# Page view tracking model
class PageView(Base):
    __tablename__ = "page_views"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String)  # URL path
    method = Column(String)  # HTTP method (GET, POST, etc.)
    ip_address = Column(String, nullable=True)  # Store IP address (consider privacy implications)
    user_agent = Column(String, nullable=True)  # Store user agent
    referer = Column(String, nullable=True)  # Store referer URL
    response_time = Column(Float, nullable=True)  # Response time in seconds
    status_code = Column(Integer, nullable=True)  # HTTP status code
    is_bot = Column(Boolean, default=False)  # Flag to indicate if the view is from a bot
    timestamp = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<PageView {self.path} at {self.timestamp}>"

# Route error tracking model
class RouteError(Base):
    __tablename__ = "route_errors"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String)  # URL path
    method = Column(String)  # HTTP method (GET, POST, etc.)
    ip_address = Column(String, nullable=True)  # Store IP address
    user_agent = Column(String, nullable=True)  # Store user agent
    error_type = Column(String)  # Exception type
    error_message = Column(Text)  # Exception message
    error_details = Column(Text, nullable=True)  # Full traceback
    response_time = Column(Float, nullable=True)  # Response time in seconds
    timestamp = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<RouteError {self.error_type} at {self.path}>"
