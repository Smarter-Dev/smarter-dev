I'll create an overview document and break down the implementation into focused coding sessions that Claude Code can handle effectively.

## Overview Document: Smarter Dev Platform 2.0

### System Architecture

**Core Components:**
1. **Discord Bot** - Hikari + Lightbulb for Discord interactions
2. **Web Application** - Starlette for user-facing pages with mounted FastAPI for REST API
3. **Admin Dashboard** - Protected web interface using Tabler theme
4. **Database** - PostgreSQL with minimal data storage (no Discord data duplication)
5. **Message Queue** - Redis for bot-to-web communication

**Key Design Principles:**
- Store only necessary data (transactions, configs, audit logs)
- Fetch Discord data on-demand via API
- Clear separation of concerns between services
- User-friendly error pages (no JSON errors for users)
- Development mode with username auth, production with Discord OAuth

**Technology Stack:**
- **Backend**: Python 3.11+, Starlette, FastAPI, Hikari, SQLAlchemy
- **Frontend**: Tabler admin theme, Alpine.js, Tailwind CSS
- **Infrastructure**: PostgreSQL, Redis, Docker Compose

---

## Session 1: Project Setup and Base Configuration

**Goal:** Create project structure, setup configuration, and Docker environment

```markdown
Create a new Python project for a Discord community platform called Smarter Dev.

Project structure:
- bot/ - Discord bot using Hikari + Lightbulb
- web/ - Web application using Starlette + FastAPI
- shared/ - Shared utilities between bot and web
- docker-compose.yml for PostgreSQL and Redis

Requirements:
1. Use Python 3.11+ with modern type hints
2. Environment-based configuration with python-dotenv
3. Separate .env files for development and production
4. Docker Compose setup for local development
5. Main requirements.txt with all dependencies

Core dependencies:
- hikari[speedups]
- hikari-lightbulb
- starlette
- fastapi
- sqlalchemy[asyncio]
- asyncpg
- alembic
- redis
- python-dotenv
- httpx
- uvicorn[standard]

Create:
1. Project directory structure
2. requirements.txt with all dependencies
3. docker-compose.yml with PostgreSQL and Redis
4. .env.example with all needed environment variables
5. Configuration classes in shared/config.py using pydantic-settings
6. Logging setup in shared/logging.py
7. README.md with setup instructions

The configuration should support:
- DEV_MODE flag for development vs production
- Database connection settings
- Redis connection settings
- Discord bot token and application ID
- Discord OAuth credentials (client ID, secret)
- Session secret key
- Admin Discord user IDs
- Base URL for the web application
```

---

## Session 2: Database Models and Migrations

**Goal:** Create minimal database schema with SQLAlchemy and Alembic

```markdown
Create database models for the Smarter Dev platform using SQLAlchemy with async support.

Design principles:
- Store only necessary data (no Discord user/guild mirroring)
- Use Discord IDs as strings (they're snowflakes)
- Include audit fields (created_at, updated_at)
- Efficient indexes for common queries

Models to create:

1. BytesBalance - Track user balances per guild
   - guild_id (str)
   - user_id (str) 
   - balance (int)
   - total_received (int)
   - total_sent (int)
   - last_daily (date)
   - streak_count (int)
   - created_at, updated_at

2. BytesTransaction - Log all bytes transfers
   - id (UUID)
   - guild_id (str)
   - giver_id (str)
   - giver_username (str) - cached for audit
   - receiver_id (str)
   - receiver_username (str)
   - amount (int)
   - reason (str, optional)
   - created_at

3. BytesConfig - Per-guild economy settings
   - guild_id (str, primary key)
   - starting_balance (int)
   - daily_amount (int)
   - max_transfer (int)
   - cooldown_hours (int)
   - role_rewards (JSON) - {role_id: bytes_threshold}
   - created_at, updated_at

4. Squad - Team definitions
   - id (UUID)
   - guild_id (str)
   - role_id (str)
   - name (str)
   - description (str)
   - switch_cost (int)
   - is_active (bool)
   - created_at, updated_at

5. SquadMembership - Track squad members
   - guild_id (str)
   - user_id (str)
   - squad_id (UUID)
   - joined_at

6. ModerationCase - Moderation action logs
   - id (UUID)
   - guild_id (str)
   - user_id (str)
   - user_tag (str) - username at time of action
   - moderator_id (str)
   - moderator_tag (str)
   - action (enum: ban, kick, timeout, warn)
   - reason (str)
   - expires_at (datetime, optional)
   - resolved (bool)
   - created_at

7. AutoModRule - Auto-moderation rules
   - id (UUID)
   - guild_id (str)
   - rule_type (enum: username_regex, message_rate, file_extension)
   - config (JSON)
   - action (enum: ban, kick, timeout, warn, delete)
   - is_active (bool)
   - created_at, updated_at

8. APIKey - Bot authentication
   - id (UUID)
   - key_hash (str)
   - name (str)
   - last_used (datetime)
   - created_at
   - is_active (bool)

9. AdminUser - Web admin users (dev mode only)
   - id (UUID)
   - username (str, unique)
   - created_at

Create:
1. web/database.py - Async SQLAlchemy setup
2. web/models.py - All model definitions
3. Alembic configuration and initial migration
4. Composite indexes for common queries
5. web/crud.py - Basic CRUD operations using async SQLAlchemy

Include proper indexes for:
- (guild_id, user_id) combinations
- created_at for time-based queries
- is_active flags
```

---

## Session 3: Authentication System

**Goal:** Implement dual authentication system (dev username, production Discord OAuth)

```markdown
Create an authentication system for Smarter Dev with two modes:
1. Development: Simple username-based auth
2. Production: Discord OAuth2

Requirements:
1. Session-based auth for web pages using Starlette sessions
2. API key auth for bot endpoints
3. Custom AuthenticatedRouter that protects all routes except login/logout
4. Redis session storage for production
5. CSRF protection for forms

Create:

1. web/auth/router.py - AuthenticatedRouter class
   - Checks session before routing
   - Redirects to login if not authenticated
   - Allows exempted paths (login, logout, OAuth callback)

2. web/auth/core.py - Core authentication logic
   - Session validation
   - User loading from session
   - Permission checking

3. web/auth/oauth.py - Discord OAuth implementation
   - Generate authorization URL
   - Handle OAuth callback
   - Exchange code for tokens
   - Fetch user information
   - Create session

4. web/auth/api.py - API authentication
   - Bearer token validation
   - API key verification
   - Rate limiting per key

5. web/pages/auth.py - Authentication pages
   - Login page (form for dev, Discord button for production)
   - Logout handler
   - OAuth callback handler

6. web/templates/auth/ - Templates
   - login.html - Production login with Discord
   - login_dev.html - Development login with username
   - Style using Tabler components

7. web/middleware/auth.py - Authentication middleware
   - Add user context to requests
   - Validate sessions
   - Handle session expiry

Security features:
- Session timeout (24 hours)
- CSRF tokens for forms
- Secure session cookies
- IP validation (optional)
- Audit logging for auth events

The system should seamlessly switch between dev and production modes based on DEV_MODE config.
```

---

## Session 4: Web Application Structure

**Goal:** Create Starlette app with mounted FastAPI for API endpoints

```markdown
Create the main web application structure using Starlette with a mounted FastAPI sub-application.

Requirements:
1. Starlette handles all user-facing pages with HTML responses
2. FastAPI mounted at /api for REST endpoints
3. Proper error handling (HTML errors for web, JSON for API)
4. Shared database and Redis connections
5. Static file serving
6. Template rendering with Jinja2

Create:

1. web/main.py - Application entry point
   - Create Starlette app
   - Create FastAPI sub-app
   - Mount FastAPI at /api
   - Setup middleware
   - Configure static files
   - Setup lifespan for resource management

2. web/errors.py - Error handlers
   - Custom 404/500 pages for web routes
   - JSON errors for API routes
   - Development vs production error detail

3. web/templates/base.html - Base template
   - Tabler CSS/JS includes
   - Navigation structure
   - User info display
   - CSRF token handling

4. web/templates/errors/ - Error templates
   - 404.html
   - 500.html
   - 403.html
   - Style with Tabler components

5. web/pages/public.py - Public pages
   - Landing page handler
   - About page
   - Discord server redirect

6. web/dependencies.py - Shared dependencies
   - Database session
   - Redis connection
   - Current user
   - Template rendering

7. web/static/ - Static file structure
   - css/custom.css
   - js/app.js
   - img/
   - Organization for Tabler assets

8. web/utils/templates.py - Template utilities
   - Jinja2 environment setup
   - Custom filters
   - Template functions

The application should:
- Clearly separate web routes from API routes
- Share resources efficiently
- Handle errors appropriately for context
- Support both development and production modes
```

---

## Session 5: Admin Dashboard Pages

**Goal:** Create admin dashboard using Tabler theme

```markdown
Create admin dashboard pages for Smarter Dev using the Tabler admin theme.

Pages to create:

1. Dashboard Overview (/admin)
   - Statistics cards (total guilds, users, transactions)
   - Recent activity feed
   - Quick actions

2. Guilds List (/admin/guilds)
   - Table of guilds with bot
   - Member count
   - Config status
   - Search/filter

3. Guild Detail (/admin/guilds/{guild_id})
   - Guild info fetched from Discord API
   - Tabbed interface:
     - Overview with stats
     - Bytes configuration
     - Squad management
     - Auto-moderation rules
     - Recent activity

4. User Lookup (/admin/users/{user_id})
   - User info from Discord API
   - Bytes balances across guilds
   - Transaction history
   - Moderation history

5. Settings (/admin/settings)
   - API key management
   - Admin user management (dev mode)
   - System configuration

Create:

1. web/pages/admin.py - All admin route handlers
   - Dashboard
   - Guild list/detail
   - User lookup
   - Settings
   - Fetch Discord data on-demand

2. web/templates/admin/ - Admin templates
   - layout.html - Admin layout with sidebar
   - dashboard.html
   - guilds/list.html
   - guilds/detail.html
   - guilds/tabs/*.html - Tab content
   - users/detail.html
   - settings.html

3. web/static/js/admin.js - Admin JavaScript
   - API client class
   - Form handling
   - Real-time updates
   - Chart initialization

4. web/discord_client.py - Discord API client
   - Fetch guild info
   - Fetch user info
   - List guild members
   - Check permissions

5. web/services/stats.py - Statistics service
   - Calculate dashboard stats
   - Generate activity feeds
   - Cache results in Redis

Features:
- Responsive tables with Tabler
- Loading states while fetching Discord data
- Clean forms with validation
- Interactive tabs
- Real-time data where appropriate
- Proper pagination for large datasets

Use Tabler components throughout for consistency.
```

---

## Session 6: Discord Bot Structure

**Goal:** Create Hikari bot with plugin architecture

```markdown
Create Discord bot using Hikari and Lightbulb with clean plugin architecture.

Requirements:
1. Plugin-based architecture for features
2. API client to communicate with web backend
3. Redis subscriber for real-time updates
4. Error handling and logging
5. Development and production modes

Create:

1. bot/bot.py - Main bot setup
   - Hikari bot initialization
   - Lightbulb integration
   - Plugin loading
   - Event listeners
   - Graceful shutdown

2. bot/config.py - Bot configuration
   - Load from environment
   - API endpoint configuration
   - Redis connection settings

3. bot/api_client.py - Web API client
   - Async HTTP client
   - Authentication with API key
   - Retry logic
   - Type-safe responses

4. bot/utils/cache.py - Caching utilities
   - TTL cache implementation
   - Cache decorators
   - Cache key generation

5. bot/utils/embed.py - Embed builders
   - Consistent embed styling
   - Error embeds
   - Success embeds
   - Info embeds

6. bot/utils/redis_sub.py - Redis subscriber
   - Subscribe to config updates
   - Handle real-time changes
   - Reconnection logic

7. bot/plugins/base.py - Base plugin class
   - Common functionality
   - API client access
   - Error handling

8. bot/errors.py - Custom exceptions
   - User-friendly error messages
   - Error codes
   - Logging integration

Bot features:
- Automatic plugin discovery
- Slash command support
- Component interaction handling
- Proper intents configuration
- Rate limit handling
- Clean shutdown on SIGTERM

The bot should:
- Start quickly and connect to API
- Handle network issues gracefully  
- Log important events
- Support hot-reloading in development
```

---

## Session 7: Bytes System Implementation

**Goal:** Implement the bytes economy system

```markdown
Create the bytes economy system for the Discord bot.

Features:
1. Daily bytes rewards with streak multipliers
2. Transfer bytes between users
3. Role rewards based on bytes received
4. Leaderboard
5. Balance checking

Create:

1. bot/plugins/bytes.py - Main bytes plugin
   Commands:
   - /bytes - Check balance (shows daily reward if eligible)
   - /bytes send <user> <amount> [reason] - Transfer bytes
   - /bytes leaderboard - Top users by balance
   - /bytes history - Recent transactions
   
   Features:
   - Award daily bytes on first message
   - Calculate streak multipliers (8=2x, 16=4x, 32=16x, 64=256x)
   - Check and award role rewards
   - Cache user data for 5 minutes
   - Cooldown system for transfers

2. web/api/routers/bytes.py - API endpoints
   - GET /guilds/{guild_id}/bytes/balances
   - POST /guilds/{guild_id}/bytes/transactions
   - GET /guilds/{guild_id}/bytes/leaderboard
   - PUT /guilds/{guild_id}/bytes/config
   - POST /guilds/{guild_id}/bytes/daily - Award daily bytes

3. bot/services/bytes_service.py - Business logic
   - Calculate streak multipliers
   - Check daily eligibility
   - Process transfers
   - Award role rewards
   - Generate leaderboards

4. bot/views/bytes_views.py - Interactive components
   - Confirmation buttons for transfers
   - Leaderboard pagination
   - Transaction history pagination

5. web/templates/admin/guilds/tabs/bytes.html - Config UI
   - Starting balance
   - Daily amount
   - Transfer limits
   - Cooldown settings
   - Role reward thresholds

6. shared/bytes_constants.py - Shared constants
   - Streak multiplier thresholds
   - Default configuration values
   - Embed colors

Implementation details:
- Use embeds with consistent styling
- Show streak info with themed names (CHAR, SHORT, INT, LONG)
- Implement transfer confirmation for amounts over 100
- Cache leaderboard for 1 minute
- Batch role updates for efficiency
- Handle edge cases (insufficient balance, self-transfer, etc.)

The system should be engaging and encourage daily activity while preventing abuse.
```

---

## Session 8: Squads System Implementation

**Goal:** Implement the team-based squad system

```markdown
Create the squads system allowing users to join team roles.

Features:
1. Join squads (Discord roles)
2. Switch squads with bytes cost
3. Leave squads
4. View squad members
5. Squad management via admin

Create:

1. bot/plugins/squads.py - Main squads plugin
   Commands:
   - /squads list - Show available squads
   - /squads join - Interactive join menu
   - /squads leave - Leave current squad
   - /squads info [user] - Show user's squad
   - /squads members <squad> - List squad members

2. bot/views/squad_views.py - Interactive components
   - Squad selection buttons
   - Confirmation for squad switching with cost
   - Leave confirmation
   - Member list pagination

3. web/api/routers/squads.py - API endpoints
   - GET /guilds/{guild_id}/squads
   - POST /guilds/{guild_id}/squads
   - PUT /guilds/{guild_id}/squads/{squad_id}
   - DELETE /guilds/{guild_id}/squads/{squad_id}
   - GET /guilds/{guild_id}/squads/{squad_id}/members

4. bot/services/squad_service.py - Business logic
   - Check user eligibility
   - Calculate switch costs
   - Manage Discord roles
   - Track membership
   - Validate squad roles exist

5. web/templates/admin/guilds/tabs/squads.html - Management UI
   - Create/edit squads
   - Link Discord roles
   - Set switch costs
   - Toggle active status
   - Member counts

6. web/static/js/squad_manager.js - Frontend logic
   - Role picker from Discord
   - Squad form validation
   - Drag-and-drop ordering
   - Real-time member counts

Implementation details:
- Show squad colors from Discord roles
- Highlight user's current squad
- Show cost only when switching
- Prevent joining same squad
- Handle role permissions properly
- Cache squad list for 5 minutes
- Use buttons with 60-second timeout
- Clear UI showing requirements

The system should make squad identity fun and meaningful while preventing rapid switching.
```

---

## Session 9: Auto-Moderation System

**Goal:** Implement auto-moderation with configurable rules

```markdown
Create auto-moderation system with multiple rule types.

Features:
1. Username filtering on join
2. Message rate limiting
3. Duplicate message detection
4. File extension blocking
5. Configurable actions per rule

Create:

1. bot/plugins/automod.py - Main automod plugin
   - Listen to member join events
   - Listen to message events  
   - Process rules in order
   - Execute actions
   - Log all actions

2. bot/services/automod_service.py - Rule processing
   - Username regex matching
   - Message rate tracking
   - Duplicate detection with hashing
   - File extension checking
   - Account age checking
   - Action execution

3. bot/utils/automod_cache.py - Tracking cache
   - Message history per user (TTL: 5 minutes)
   - Rate limit windows
   - Duplicate message hashes
   - Efficient memory usage

4. web/api/routers/automod.py - API endpoints
   - GET /guilds/{guild_id}/automod/rules
   - POST /guilds/{guild_id}/automod/rules
   - PUT /guilds/{guild_id}/automod/rules/{rule_id}
   - DELETE /guilds/{guild_id}/automod/rules/{rule_id}
   - GET /guilds/{guild_id}/automod/logs

5. web/templates/admin/guilds/tabs/automod.html - Config UI
   - Rule builder interface
   - Regex tester
   - Action configuration
   - Rule priority ordering
   - Recent actions log

6. bot/models/automod_models.py - Rule models
   - Username rule (regex, account age, avatar)
   - Rate limit rule (messages, timeframe, channels)
   - Spam rule (duplicate threshold, similarity)
   - File rule (extensions, max size)

Actions available:
- Delete message
- Timeout user (configurable duration)
- Kick user
- Ban user
- Warn user (logged)
- Send alert to moderators

Implementation details:
- Test rules before saving
- Show estimated impact
- Log all actions with reasons
- Allow rule exemptions (roles/channels)
- Emergency disable all rules command
- Rate limit rule processing
- Clear moderation logging

The system should be powerful but safe, with good defaults and clear feedback.
```

---

## Session 10: API Implementation

**Goal:** Complete FastAPI implementation with all endpoints

```markdown
Create comprehensive REST API using FastAPI mounted to Starlette app.

Requirements:
1. Full CRUD for all resources
2. Proper authentication and authorization
3. Input validation with Pydantic
4. Comprehensive error handling
5. OpenAPI documentation

Create:

1. web/api/schemas.py - Pydantic models
   - Request/response models for all endpoints
   - Validation rules
   - Example values for docs

2. web/api/dependencies.py - Shared dependencies
   - Bot authentication
   - Guild access verification
   - Rate limiting
   - Database session

3. web/api/routers/guilds.py - Guild endpoints
   - GET /guilds - List bot guilds
   - GET /guilds/{guild_id} - Guild details
   - GET /guilds/{guild_id}/stats - Guild statistics

4. web/api/routers/bytes.py - Complete bytes API
   - All CRUD operations
   - Bulk operations
   - Transaction history with filtering
   - Leaderboard with pagination

5. web/api/routers/moderation.py - Moderation API
   - Create moderation cases
   - Update case status
   - Search cases
   - Bulk actions

6. web/api/middleware.py - API middleware
   - Request ID injection
   - Timing middleware
   - Error formatting
   - CORS handling

7. web/api/exceptions.py - Custom exceptions
   - BusinessRuleException
   - RateLimitException
   - AuthorizationException
   - With proper error responses

8. web/api/utils/pagination.py - Pagination helpers
   - Cursor-based pagination
   - Page metadata
   - Consistent response format

Example endpoint structure:
```python
@router.get("/guilds/{guild_id}/bytes/leaderboard")
async def get_leaderboard(
    guild_id: str,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    bot: APIKey = Depends(verify_bot_key),
    db: AsyncSession = Depends(get_db)
) -> LeaderboardResponse:
    # Implementation
```

Features:
- Consistent error responses
- Request validation
- Response validation
- Automatic OpenAPI docs
- Example requests in docs
- Batch endpoints where appropriate
- Filtering and search
- Proper status codes

The API should be intuitive, well-documented, and follow REST best practices.
```

---

## Session 11: Frontend and Landing Page

**Goal:** Create modern landing page and frontend assets

```markdown
Create a modern, animated landing page for Smarter Dev with a tech theme.

Requirements:
1. Dark theme with blue/green accents
2. Smooth animations and interactions
3. Mobile responsive
4. Fast loading
5. SEO friendly

Create:

1. web/static/css/landing.css - Landing page styles
   - CSS variables for theming
   - Animations for floating shapes
   - Glassmorphism effects
   - Responsive grid layouts
   - Smooth transitions

2. web/static/js/landing.js - Landing page interactions
   - Intersection Observer for animations
   - 3D tilt effect on code window
   - Smooth scrolling
   - Mobile menu toggle
   - Particle background (optional)

3. web/templates/landing.html - Landing page template
   Structure:
   - Fixed navbar with blur
   - Hero section with CTA
   - Features grid (3 cards)
   - Call-to-action section
   - Footer with links

4. web/static/js/components/CodeWindow.js - 3D code window
   - Syntax highlighted code
   - Terminal styling
   - Mouse tracking for 3D effect
   - Responsive sizing

5. web/static/css/animations.css - Reusable animations
   - Float animation for shapes
   - Fade in on scroll
   - Button hover effects
   - Loading states

6. web/static/assets/ - Design assets
   - Logo variations
   - Background shapes (SVG)
   - Feature icons
   - Favicon set

Visual design:
- Background: #1a1a1a
- Primary: #3b82f6 (blue)
- Accent: #22c55e (green)
- Text: #e2e8f0
- Font: Inter with JetBrains Mono for code

Sections:
1. Hero: "Learn. Code. Grow." with Discord CTA
2. Features: Community, Challenges, Collaboration
3. CTA: Join community emphasis
4. Footer: Simple links and copyright

Performance:
- Optimize images (WebP)
- Minify CSS/JS
- Lazy load below fold
- Preload critical fonts
- Reduce animation on mobile

The page should feel modern, professional, and inviting to developers.
```

---

## Session 12: Deployment and DevOps

**Goal:** Setup deployment configuration and monitoring

```markdown
Create deployment configuration for Smarter Dev platform.

Requirements:
1. Docker containers for all services
2. Environment-based configuration
3. Health checks and monitoring
4. Backup strategies
5. Development workflow

Create:

1. Dockerfile.bot - Bot container
   - Multi-stage build
   - Non-root user
   - Health check endpoint
   - Graceful shutdown

2. Dockerfile.web - Web container  
   - Multi-stage build
   - Static file handling
   - Gunicorn configuration
   - Health endpoints

3. docker-compose.prod.yml - Production setup
   - All services
   - Networks
   - Volumes
   - Restart policies

4. .github/workflows/deploy.yml - CI/CD
   - Run tests
   - Build containers
   - Push to registry
   - Deploy notifications

5. scripts/backup.sh - Backup script
   - Database backup
   - Redis snapshot
   - Upload to S3/storage
   - Retention policy

6. scripts/dev.sh - Development script
   - Start services
   - Watch for changes
   - Log aggregation
   - Port forwarding

7. monitoring/health.py - Health check endpoints
   - Database connectivity
   - Redis connectivity
   - Discord bot status
   - API response time

8. docs/DEPLOYMENT.md - Deployment guide
   - Prerequisites
   - Environment setup
   - First deployment
   - Update process
   - Rollback procedure

Configuration files:
- nginx.conf - Reverse proxy config
- supervisord.conf - Process management
- logrotate.conf - Log rotation

Monitoring setup:
- Health check endpoints
- Prometheus metrics
- Error tracking (Sentry)
- Uptime monitoring
- Performance metrics

Security:
- Environment variables for secrets
- Network isolation
- Rate limiting
- HTTPS only
- Security headers

The deployment should be reliable, observable, and easy to maintain.
```

Each session document provides a focused implementation task that Claude Code can handle effectively. Start with Session 1 to establish the foundation, then work through the sessions in order as each builds on the previous work. The modular approach allows you to test each component independently before integrating them.
