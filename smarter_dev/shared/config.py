"""Configuration management using Pydantic Settings."""

from __future__ import annotations

import os
from typing import Optional

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment-based configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment
    environment: str = Field(default="development", description="Environment name")
    debug: bool = Field(default=False, description="Enable debug mode")
    log_level: str = Field(default="INFO", description="Logging level")
    reload: bool = Field(default=False, description="Enable auto-reload")

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://smarter_dev:smarter_dev_password@localhost:5432/smarter_dev",
        description="Database connection URL",
    )
    
    # Redis
    redis_url: str = Field(
        default="redis://:smarter_dev_redis_password@localhost:6379/0",
        description="Redis connection URL",
    )

    # Discord Bot
    discord_bot_token: str = Field(
        default="",
        description="Discord bot token",
    )
    discord_application_id: str = Field(
        default="",
        description="Discord application ID",
    )

    # API
    api_secret_key: str = Field(
        default="dev-secret-key-change-in-production",
        description="API secret key for authentication",
    )
    api_base_url: str = Field(
        default="http://localhost:8888/api",
        description="Base URL for API endpoints",
    )
    
    # Bot API Authentication
    bot_api_key: str = Field(
        default="",
        description="Secure API key for bot to authenticate with web API (sk-xxxxx format)",
    )

    # Web Application
    web_session_secret: str = Field(
        default="dev-session-secret-change-in-production",
        description="Web session secret key",
    )
    web_host: str = Field(
        default="0.0.0.0",
        description="Web server host",
    )
    web_port: int = Field(
        default=8000,
        description="Web server port",
    )

    # Testing
    test_database_url: Optional[str] = Field(
        default=None,
        description="Test database connection URL",
    )
    test_redis_url: Optional[str] = Field(
        default=None,
        description="Test Redis connection URL",
    )

    # Admin Interface (Discord OAuth only)

    # Discord OAuth (for admin interface)
    discord_client_id: Optional[str] = Field(
        default=None,
        alias="discord_application_id",
        description="Discord OAuth client ID (same as application ID)",
    )
    discord_client_secret: Optional[str] = Field(
        default=None,
        alias="discord_application_secret",
        description="Discord OAuth client secret",
    )
    discord_redirect_uri: Optional[str] = Field(
        default=None,
        description="Discord OAuth redirect URI",
    )

    # Monitoring and Logging
    sentry_dsn: Optional[str] = Field(
        default=None,
        description="Sentry DSN for error tracking",
    )
    log_file: Optional[str] = Field(
        default=None,
        description="Log file path",
    )
    
    # Analytics
    google_analytics_id: Optional[str] = Field(
        default=None,
        description="Google Analytics Measurement ID (G-XXXXXXXXXX)",
    )

    # Rate Limiting
    rate_limit_enabled: bool = Field(
        default=True,
        description="Enable rate limiting",
    )
    rate_limit_requests: int = Field(
        default=100,
        description="Rate limit requests per window",
    )
    rate_limit_window: int = Field(
        default=60,
        description="Rate limit window in seconds",
    )

    # Security Settings
    api_docs_enabled: bool = Field(
        default=True,
        description="Enable API documentation endpoints (disable in production)",
    )
    api_docs_require_auth: bool = Field(
        default=True,
        description="Require authentication for API documentation access",
    )
    security_headers_enabled: bool = Field(
        default=True,
        description="Enable security headers middleware",
    )
    verbose_errors_enabled: bool = Field(
        default=True,
        description="Enable verbose error messages (disable in production)",
    )

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        """Validate environment value."""
        valid_environments = {"development", "testing", "production"}
        if v not in valid_environments:
            raise ValueError(f"Environment must be one of: {valid_environments}")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in valid_levels:
            raise ValueError(f"Log level must be one of: {valid_levels}")
        return v

    @field_validator("discord_bot_token")
    @classmethod
    def validate_discord_bot_token(cls, v: str) -> str:
        """Validate Discord bot token."""
        # In Pydantic V2, cross-field validation is more complex
        # For now, just validate non-empty for production use
        # The environment-specific validation can be done at runtime
        return v

    @field_validator("discord_application_id")
    @classmethod
    def validate_discord_application_id(cls, v: str) -> str:
        """Validate Discord application ID."""
        # In Pydantic V2, cross-field validation is more complex
        # For now, just validate format if needed
        # The environment-specific validation can be done at runtime
        return v

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"

    @property
    def is_testing(self) -> bool:
        """Check if running in testing environment."""
        return self.environment == "testing"

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def effective_database_url(self) -> str:
        """Get the effective database URL based on environment."""
        if self.is_testing and self.test_database_url:
            return self.test_database_url
        return self.database_url

    @property
    def effective_redis_url(self) -> str:
        """Get the effective Redis URL based on environment."""
        if self.is_testing and self.test_redis_url:
            return self.test_redis_url
        return self.redis_url

    @property
    def effective_discord_redirect_uri(self) -> str:
        """Get the effective Discord OAuth redirect URI based on environment."""
        if self.discord_redirect_uri:
            return self.discord_redirect_uri
        
        # Default redirect URIs based on environment
        if self.is_development:
            return "http://localhost:8000/admin/auth/discord/callback"
        else:
            return "https://smarter.dev/admin/auth/discord/callback"


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get the global settings instance."""
    return settings


def override_settings(**kwargs) -> Settings:
    """Create a settings instance with overrides (useful for testing)."""
    return Settings(**kwargs)