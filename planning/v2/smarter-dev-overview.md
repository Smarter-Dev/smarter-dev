# Smarter Dev 2.0 - Project Overview

## Executive Summary

Smarter Dev is a comprehensive Discord community management platform consisting of a Discord bot and web application. This document outlines a complete rewrite focusing on improved architecture, maintainability, and user experience.

## Core Architecture Principles

### 1. Data Management
- **No Discord Data Duplication**: Store only platform-specific data (transactions, configurations, audit logs)
- **Discord as Source of Truth**: Fetch user/guild data on-demand via Discord API
- **Efficient Caching**: 5-minute TTL caches for frequently accessed data

### 2. Service Architecture
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Discord Bot    │────▶│   Redis Queue   │◀────│  Web Backend    │
│  (Hikari)       │     │   (Pub/Sub)     │     │  (Starlette)    │
└────────┬────────┘     └─────────────────┘     └────────┬────────┘
         │                                                │
         └──────────────▶ PostgreSQL ◀────────────────────┘
```

### 3. Technology Stack
- **Bot Framework**: Hikari + Lightbulb (high-performance Discord bot)
- **Web Framework**: Starlette (user pages) + mounted FastAPI (REST API)
- **Database**: PostgreSQL with SQLAlchemy ORM + Alembic migrations
- **Cache/Queue**: Redis for caching and bot-web communication
- **Authentication**: Dev mode (username) / Production (Discord OAuth2)
- **Frontend**: Tabler admin theme, Alpine.js for interactivity
- **Testing**: pytest with httpx.AsyncClient for web, service layer testing for bot

## Key Features

### 1. Bytes Economy System
- Virtual currency system with daily rewards and streaks
- Programming-themed multipliers (CHAR, SHORT, INT, LONG)
- Role rewards based on bytes received
- Transfer system with cooldowns and limits

### 2. Squads System
- Team-based roles users can join
- Economic cost for switching squads
- Visual squad identity through Discord roles

### 3. Auto-Moderation
- Username filtering with regex patterns
- Message rate limiting and spam detection
- File extension blocking
- Configurable actions (ban, kick, timeout, warn)

### 4. Admin Dashboard
- Real-time Discord data integration
- Guild and user management
- Configuration interfaces for all features
- Analytics and activity monitoring

## Database Schema (Minimal)

### Core Tables
1. **BytesBalance** - User balances per guild
2. **BytesTransaction** - Audit log of all transfers
3. **BytesConfig** - Per-guild economy settings
4. **Squad** - Team definitions with Discord role binding
5. **SquadMembership** - Current squad memberships
6. **ModerationCase** - Moderation action logs
7. **AutoModRule** - Configurable moderation rules
8. **AdminUser** - Web admin users (dev mode)
9. **APIKey** - Bot authentication tokens

## Testing Strategy

### Web Application Testing
- Use `httpx.AsyncClient` with app transport
- No special test logic in application code
- Separate test database with migrations
- Mock Discord API responses

### Bot Testing Strategy
- **Layered Architecture**: Commands/listeners are thin wrappers
- **Service Layer**: All business logic in testable service classes
- **Mock Framework**: Create mock contexts and events for unit tests
- **Integration Tests**: Optional with test bot token

Example bot architecture:
```python
# Thin command layer
@bot.command
async def bytes(ctx: Context) -> None:
    result = await bytes_service.check_balance(ctx.guild_id, ctx.author.id)
    await ctx.respond(embed=result.to_embed())

# Testable service layer
class BytesService:
    async def check_balance(self, guild_id: str, user_id: str) -> BalanceResult:
        # All business logic here, easily testable
```

## Development Workflow

### Local Environment
- Docker Compose (using podman compose) for PostgreSQL and Redis
- Separate .env files for bot and web services
- Hot reloading for both services
- Unified logging to console

### Configuration
- Environment-based configuration with python-dotenv
- DEV_MODE flag for development features
- Separate configs for bot and web services

## Implementation Phases

1. **Setup & Infrastructure** - Project setup, Docker environment, base configuration
2. **Database & Models** - Schema design, migrations, CRUD operations
3. **Authentication System** - Dual-mode auth (dev/production)
4. **Web Application Core** - Starlette + FastAPI structure
5. **Admin Dashboard** - Tabler integration, management pages
6. **Discord Bot Core** - Hikari setup, plugin architecture
7. **Bytes System** - Complete economy implementation
8. **Squads System** - Team management features
9. **Auto-Moderation** - Rule engine and actions
10. **API Implementation** - Complete REST API
11. **Frontend Polish** - Landing page, animations
12. **Testing Suite** - Comprehensive test coverage

## Key Improvements Over Existing System

1. **No Data Duplication**: Significant reduction in database complexity
2. **Better Error UX**: HTML error pages for users, JSON for API
3. **Clean Architecture**: Clear separation between services
4. **Type Safety**: Full type hints and Pydantic validation
5. **Testability**: Service layer pattern for easy testing
6. **Performance**: Efficient caching and query optimization

## Security Considerations

- API key authentication for bot-to-web communication
- Session-based auth for web with CSRF protection
- Rate limiting on all endpoints
- Input validation with Pydantic
- Secure Discord OAuth2 flow
- No storage of Discord tokens

## Success Criteria

1. All existing features reimplemented with improved architecture
2. 80%+ test coverage on business logic
3. Sub-100ms response times for cached operations
4. Clean separation allowing independent service updates
5. Comprehensive documentation for operators

This project represents a complete architectural overhaul while maintaining the features that make Smarter Dev valuable to the community. The focus is on maintainability, performance, and developer experience.