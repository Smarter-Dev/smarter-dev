Looking at your current implementation, I can see you've built a comprehensive Discord community platform with some architectural decisions that are causing friction. Let me provide a refined plan to rebuild this with better separation of concerns and a more maintainable architecture.

## Smarter Dev Platform Rebuild Plan

### Core Architecture Principles

1. **Minimize Data Duplication**: Store only what's necessary (audit logs, transactions, configurations) and fetch Discord data on-demand
2. **Clear Service Boundaries**: Bot handles Discord interactions, API serves data, frontend consumes both
3. **Event-Driven Design**: Use webhooks and message queues for async operations
4. **Graceful Error Handling**: User-friendly error pages instead of raw JSON responses

### Technology Stack

#### Backend Services
- **Web Framework**: Starlette (avoid FastAPI's JSON error responses)
- **Discord Bot**: Hikari + Lightbulb
- **Database**: PostgreSQL with SQLAlchemy ORM
- **Message Queue**: Redis (for bot-to-web communication)
- **Authentication**: 
  - Development: Simple username auth
  - Production: Discord OAuth2
- **API Documentation**: OpenAPI schema with ReDoc

#### Frontend Stack
- **Landing Page**: Custom HTML/CSS/JS with modern animations
- **Admin Dashboard**: [Tabler](https://tabler.io/) (clean, professional, free)
- **Build Tools**: Vite for asset bundling
- **CSS Framework**: Tailwind CSS for consistency
- **JavaScript**: Alpine.js for lightweight interactivity

### Database Schema (Minimal)

```python
# Only store what Discord doesn't already have
class BytesTransaction:
    id: UUID
    guild_id: str  # Discord guild ID
    giver_id: str  # Discord user ID
    giver_username: str  # Cached for audit
    receiver_id: str
    receiver_username: str
    amount: int
    reason: str
    timestamp: datetime

class BytesBalance:
    guild_id: str
    user_id: str
    balance: int
    total_received: int
    total_sent: int
    last_daily: date
    streak_count: int

class BytesConfig:
    guild_id: str
    starting_balance: int
    daily_amount: int
    max_transfer: int
    cooldown_hours: int
    role_rewards: JSON  # {role_id: bytes_required}

class Squad:
    id: UUID
    guild_id: str
    role_id: str  # Discord role ID
    name: str
    description: str
    switch_cost: int
    is_active: bool

class SquadMembership:
    guild_id: str
    user_id: str
    squad_id: UUID
    joined_at: datetime

class ModerationCase:
    id: UUID
    guild_id: str
    user_id: str
    user_tag: str  # Username#discriminator at time of action
    moderator_id: str
    moderator_tag: str
    action: str  # ban, kick, timeout, warn
    reason: str
    timestamp: datetime
    resolved: bool

class AutoModRule:
    id: UUID
    guild_id: str
    rule_type: str  # username_regex, message_rate, file_extension
    config: JSON
    action: str
    is_active: bool
```

### Service Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│                 │     │                 │     │                 │
│  Discord Bot    │────▶│   Redis Queue   │◀────│  Web Backend    │
│  (Hikari)       │     │                 │     │  (Starlette)    │
│                 │     │                 │     │                 │
└────────┬────────┘     └─────────────────┘     └────────┬────────┘
         │                                                │
         │              ┌─────────────────┐               │
         └─────────────▶│                 │◀──────────────┘
                        │   PostgreSQL    │
                        │                 │
                        └─────────────────┘
```

### Implementation Plan

#### Phase 1: Core Infrastructure (Week 1)
1. **Database Setup**
   - Design minimal schema (no Discord data duplication)
   - Setup Alembic migrations
   - Create SQLAlchemy models

2. **Authentication Service**
   - Username auth for development
   - Discord OAuth2 flow for production
   - JWT tokens for API authentication

3. **Base Web Application**
   - Starlette app with error handling middleware
   - Custom error pages (404, 500, etc.)
   - Session management
   - Static file serving with Vite

#### Phase 2: Discord Bot Core (Week 2)
1. **Bot Framework**
   - Hikari + Lightbulb setup
   - Plugin architecture
   - Redis publisher for events
   - Configuration from database

2. **Bytes System**
   ```python
   # Example plugin structure
   @lightbulb.Plugin
   class BytesPlugin:
       def __init__(self):
           self.cache = TTLCache(ttl=300)  # 5-minute cache
           
       @lightbulb.command
       async def bytes(self, ctx):
           # Fetch balance from DB
           # Check cache first
           # Award daily bytes if eligible
           # Publish event to Redis
   ```

3. **Basic Commands**
   - `/bytes` - Check balance
   - `/bytes send` - Transfer bytes
   - `/bytes leaderboard` - Top users

#### Phase 3: Admin Dashboard (Week 3)
1. **Dashboard Layout**
   - Integrate Tabler theme
   - Navigation structure
   - Real-time Discord data fetching

2. **Guild Management**
   ```python
   @router.get("/guilds/{guild_id}")
   async def guild_dashboard(guild_id: str):
       # Fetch guild info from Discord API
       guild = await discord_client.fetch_guild(guild_id)
       
       # Fetch only our data from DB
       config = await db.get_bytes_config(guild_id)
       stats = await db.get_guild_stats(guild_id)
       
       return templates.render("guild_dashboard.html", {
           "guild": guild,  # Real-time Discord data
           "config": config,  # Our stored config
           "stats": stats     # Our calculated stats
       })
   ```

3. **Configuration Pages**
   - Bytes economy settings
   - Squad management
   - Auto-moderation rules
   - Role rewards

#### Phase 4: Advanced Features (Week 4)
1. **Squad System**
   - Join/leave/switch squads
   - Cost management
   - Role synchronization

2. **Auto-Moderation**
   - Regex username filters
   - Rate limiting
   - File extension blocking
   - Action logging

3. **Analytics Dashboard**
   - Command usage stats
   - Bytes economy health
   - Moderation metrics
   - Real-time activity graphs

### API Design

```python
# RESTful API with Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse

async def get_guild_config(request):
    guild_id = request.path_params['guild_id']
    
    # Verify bot is in guild via Discord API
    try:
        guild = await discord_client.fetch_guild(guild_id)
    except NotFound:
        return JSONResponse({"error": "Guild not found"}, 404)
    
    # Fetch our config
    config = await db.get_bytes_config(guild_id)
    return JSONResponse(config.dict())

routes = [
    Mount('/api', routes=[
        Route('/guilds/{guild_id}/config', get_guild_config),
        Route('/guilds/{guild_id}/bytes/transactions', get_transactions),
        Route('/guilds/{guild_id}/squads', manage_squads, methods=['GET', 'POST']),
    ])
]
```

### Frontend Guidelines

#### Landing Page
- Keep the existing design but rebuild with:
  - Vanilla JS or Alpine.js for interactivity
  - CSS animations for floating shapes
  - Intersection Observer for scroll animations
  - Optimized assets (WebP images, minified CSS/JS)

#### Admin Dashboard
- Use Tabler's components consistently
- Real-time updates via WebSockets for active data
- Skeleton loaders while fetching Discord data
- Clear visual hierarchy with cards and sections

### Error Handling Strategy

```python
# Custom error handler for Starlette
async def error_handler(request, exc):
    if isinstance(exc, HTTPException):
        if request.url.path.startswith('/api'):
            return JSONResponse({
                "error": exc.detail,
                "status": exc.status_code
            }, status_code=exc.status_code)
        else:
            return templates.render(f"errors/{exc.status_code}.html", {
                "request": request,
                "error": exc.detail
            }, status_code=exc.status_code)
```

### Development Workflow

1. **Local Development**
   - Docker Compose for PostgreSQL and Redis
   - Hot reload for both bot and web
   - Separate `.env` files for each service

2. **Testing Strategy**
   - Unit tests for business logic
   - Integration tests for API endpoints
   - Mock Discord API for testing

3. **Deployment**
   - Separate containers for bot and web
   - Shared Redis and PostgreSQL
   - Environment-based configuration

### Key Improvements Over Current System

1. **No Data Duplication**: Fetch Discord data on-demand instead of syncing everything
2. **Better Error UX**: Custom error pages instead of JSON responses
3. **Cleaner Architecture**: Clear separation between bot, API, and frontend
4. **Scalability**: Redis queue handles async communication between services
5. **Maintainability**: Plugin-based bot architecture, themed dashboard
6. **Performance**: Minimal database queries, efficient caching strategy

This rebuild plan focuses on creating a more maintainable, scalable system while keeping all the features that make Smarter Dev valuable to your community. The key is treating Discord as the source of truth for user/guild data while only storing what's unique to your platform.
