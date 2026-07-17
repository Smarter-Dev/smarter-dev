"""Configuration management using Pydantic Settings."""

from __future__ import annotations

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

    # Discord Voice Messages
    voice_tts_model: str = Field(
        default="gemini-2.5-flash-preview-tts",
        description="Gemini TTS model for Discord voice messages",
    )
    voice_tts_voice: str = Field(
        default="Kore",
        description="Gemini prebuilt voice name for Discord voice messages",
    )
    voice_tts_sample_rate: int = Field(
        default=24000,
        description="PCM sample rate returned by the configured TTS model",
    )
    voice_tts_channels: int = Field(
        default=1,
        description="PCM channel count returned by the configured TTS model",
    )
    voice_tts_sample_width: int = Field(
        default=2,
        description="PCM sample width in bytes returned by the configured TTS model",
    )
    voice_max_input_chars: int = Field(
        default=800,
        description="Maximum response characters sent to TTS for one voice message",
    )
    voice_words_per_minute: int = Field(
        default=150,
        description="Estimated spoken words per minute for voice response budgeting",
    )
    voice_max_duration_seconds: int = Field(
        default=30,
        description="Target maximum spoken duration for one voice response",
    )
    voice_opus_bitrate: str = Field(
        default="48k",
        description="Opus bitrate used when encoding Discord voice messages",
    )

    # Bot health-check HTTP server port (override locally to avoid conflicts)
    bot_health_port: int = Field(
        default=8080,
        description="Port for the bot's health-check HTTP server",
    )

    # Agentic handler system
    handlers_enabled: bool = Field(
        default=True,
        description="Master kill switch for the agentic handler system",
    )
    handler_author_model: str = Field(
        default="gemini-3-flash-preview",
        description="Model that writes handler scripts from a description (Gemini 3 Flash)",
    )
    handler_judge_model: str = Field(
        default="gemini-3-flash-preview",
        description="Model that reviews candidate handler scripts (Gemini 3 Flash)",
    )
    handler_admin_second_judge_model: str = Field(
        default="gemini-3.5-flash",
        description="Second judge for ADMIN handlers — reviews in series with the "
        "primary judge and either rejection blocks install (their observed blind "
        "spots don't overlap). Empty string disables the second judge.",
    )

    # Digital Ocean serverless inference (OpenAI-compatible). Hosts the
    # Kimi/GLM/DeepSeek/Gemma/Qwen catalog models. The secret is read from the
    # DIGITALOCEAN_INFERENCE_API_KEY env var in model_router (matching the other
    # LLM keys), not from this class; only the endpoint is configured here.
    digitalocean_inference_base_url: str = Field(
        default="https://inference.do-ai.run/v1",
        description="Base URL for Digital Ocean's OpenAI-compatible serverless "
        "inference endpoint",
    )

    # API
    api_secret_key: str = Field(
        default="dev-secret-key-change-in-production",
        description="API secret key for authentication",
    )
    api_base_url: str = Field(
        default="http://localhost:8000/api",
        description="Base URL for API endpoints",
    )
    
    # Bot API Authentication
    bot_api_key: str = Field(
        default="",
        description=(
            "Secure API key for the bot to authenticate with the web API "
            "(a Skrift-native 'sk_' key; see "
            "docs/v2/legacy-sunset/runbooks/01-rotate-bot-key.md)."
        ),
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
    
    # Email (Resend)
    resend_api_key: Optional[str] = Field(
        default=None,
        description="Resend API key for transactional email",
    )
    site_base_url: str = Field(
        default="http://localhost:8000",
        description="Public base URL for the site (used in confirmation links)",
    )

    # Analytics
    google_analytics_id: Optional[str] = Field(
        default=None,
        description="Google Analytics Measurement ID (G-XXXXXXXXXX)",
    )

    # Polar / sudo membership billing
    # The offering catalog (products, prices, perks) lives in Polar — see
    # smarter_dev.web.billing.catalog. Only the access token, webhook signing
    # secret, and environment are configured here; product IDs are discovered
    # from the API.
    polar_access_token: Optional[str] = Field(
        default=None,
        description="Polar organization access token (polar_oat_xxx)",
    )
    polar_webhook_secret: Optional[str] = Field(
        default=None,
        description="Polar webhook signing secret (standard-webhooks base64 secret)",
    )
    polar_server: str = Field(
        default="production",
        description="Polar API environment: 'production' or 'sandbox'",
    )
    polar_organization_id: Optional[str] = Field(
        default=None,
        description=(
            "Polar organization id. Optional — an organization access token is "
            "already org-scoped; only needed when seeding with a token that can "
            "see multiple organizations."
        ),
    )

    # NOTE: Sudo Discord projection IDs (guild + base + offering roles) live
    # on the Polar Product metadata, not in settings. Seed via
    # ``scripts/seed_polar_catalog.py`` and consume via
    # ``smarter_dev.web.billing.catalog.get_discord_config``.

    # Quests
    quest_timezone: str = Field(
        default="UTC",
        description="Timezone for quest date calculations (e.g., America/Chicago)",
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
            return "http://localhost:8000/bot-admin/auth/discord/callback"
        else:
            return "https://smarter.dev/bot-admin/auth/discord/callback"


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get the global settings instance."""
    return settings


def override_settings(**kwargs) -> Settings:
    """Create a settings instance with overrides (useful for testing)."""
    return Settings(**kwargs)
