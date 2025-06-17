# Smarter Dev Project Features Documentation

Based on my analysis of your codebase, here's a comprehensive documentation of all features in the Smarter Dev project:

## Project Overview
**Smarter Dev** is a comprehensive Discord community management platform consisting of a Discord bot and web application for managing Discord server activities, user engagement, and moderation.

## Core Components

### 1. Discord Bot Features

#### **Bytes System** (`bot/plugins/bytes.py`)
A comprehensive virtual economy system for Discord servers that gamifies user engagement through a programming-themed currency system.

**How it Works:**
- **Virtual Currency**: Users earn and exchange "bytes" as a recognition system
- **Daily Rewards**: When users send their first message each day, they automatically receive daily bytes (default: 10)
- **Streak System**: Consecutive daily activity increases rewards with programming-themed multipliers:
  - 8+ days (CHAR): 2x multiplier
  - 16+ days (SHORT): 4x multiplier  
  - 32+ days (INT): 16x multiplier
  - 64+ days (LONG): 256x multiplier
- **Role Rewards**: Users automatically receive Discord roles based on total bytes received
- **Transfer System**: Users can send bytes to each other with configurable limits and cooldowns

**Commands:**
- `/bytes send <user> <amount> [reason]` - Transfer bytes to other users
- `/bytes read [user]` - Check user's bytes balance, received, sent, and earned roles
- `/bytes awards [user]` - Manually check and assign any earned roles
- `/bytes heap [limit]` - View server leaderboard (top users by bytes balance)

**Technical Features:**
- **Smart Caching**: 5-minute cache for user data, configuration, and eligibility to reduce API calls
- **Cooldown System**: Configurable per-server cooldowns (default: 24 hours) prevent spam
- **Duplicate Prevention**: Multiple safeguards prevent users from receiving daily bytes multiple times
- **Fresh Data Retrieval**: Commands can bypass cache when up-to-date data is critical

#### **Squads System** (`bot/plugins/squads.py`)
A team-based role system that allows users to join groups within Discord servers, with economic costs for switching.

**How it Works:**
- **Team Formation**: Users can join squads (Discord role-based teams) created by server admins
- **Single Membership**: Users can only be in one squad per server at a time
- **Squad Switching**: Users can switch squads but pay a configurable bytes cost (default: 50 bytes)
- **Eligibility Requirements**: Minimum bytes balance required to use squad commands (configurable per server)
- **Automatic Role Management**: Bot automatically adds/removes Discord roles when users join/leave squads

**Commands:**
- `/squads list` - View all available squads in the server with descriptions
- `/squads join` - Interactive button interface to join a squad (shows switching costs if applicable)
- `/squads leave` - Interactive button interface to leave current squad
- `/squads info [user]` - View user's current squad memberships and eligible squads
- `/squads members <squad_name>` - List all members of a specific squad

**Interactive Features:**
- **Button-based UI**: All squad operations use Discord's interactive buttons with 60-second timeouts
- **Confirmation System**: Squad switching requires confirmation with cost display
- **Real-time Updates**: Squad membership changes are immediately reflected in Discord roles

#### **Auto-Moderation** (`bot/plugins/automod.py`)
An intelligent moderation system that automatically detects and responds to various types of problematic behavior.

**How it Works:**

**Username Filtering (On Member Join):**
- **Regex Pattern Matching**: Configurable regex patterns to detect problematic usernames
- **Account Age Filtering**: Rules can target accounts newer than specified days
- **Avatar Requirements**: Rules can target users without custom avatars (default Discord avatars)
- **Immediate Action**: Automatic ban, kick, timeout, or warn when users join with flagged usernames

**Message-Based Rate Limiting:**
- **Message Count Limits**: Detects users sending too many messages in a time period
- **Duplicate Message Detection**: Identifies users repeating the same message content
- **Channel Spam Detection**: Flags users posting across too many channels rapidly
- **Smart Content Hashing**: Uses content hashing to detect similar messages

**Moderation Actions:**
- **Ban**: Permanently removes user from server
- **Kick**: Removes user but allows rejoining
- **Timeout**: Temporarily mutes user for specified duration
- **Warn**: Issues warning and logs to moderation system

**Technical Features:**
- **Real-time Tracking**: Maintains message history per user with automatic cleanup
- **Configurable Rules**: All limits, timeframes, and actions configurable per server
- **Cache System**: 5-minute cache for rules and guild data to optimize performance
- **Batch Processing**: Efficiently handles high-traffic servers

#### **API Synchronization** (`bot/api_sync.py`, `bot/api_client.py`)
A robust system that keeps Discord data synchronized with the web application database in real-time.

**How it Works:**
- **Event-Driven Sync**: Automatically syncs when Discord events occur (user joins, updates, etc.)
- **Guild Synchronization**: When bot joins a server, all members are synced in batches of 100
- **User Profile Updates**: Username, avatar, and profile changes sync automatically
- **Batch Processing**: Efficient bulk operations for large servers (10,000+ members)
- **Connection Pooling**: Optimized HTTP connections with concurrency limits
- **Automatic Token Refresh**: JWT tokens refreshed automatically when expired
- **Exponential Backoff**: Retry failed requests with increasing delays
- **Manual Sync Command**: `/sync` command for administrators to force synchronization

### 2. Web Application Features

#### **Admin Dashboard** (`website/admin_routes.py`)
A comprehensive web interface for managing the entire platform with analytics and administration tools.

**How it Works:**
- **Analytics Dashboard**: Real-time page views, error tracking, and usage statistics
- **URL Redirect Management**: Create short URLs (smarter.dev/shortname) that redirect to longer URLs
- **Database Maintenance Tools**: Direct database operations for advanced administration
- **Error Monitoring**: Detailed error logs with stack traces and request information
- **Click Tracking**: Monitor redirect usage with IP, user agent, and referrer data

#### **Discord Admin Interface** (`website/discord_admin_routes.py`)
Web-based controls for managing Discord bot behavior and server settings.

**How it Works:**
- **Guild Management**: View all connected Discord servers with member counts and settings
- **User Profiles**: Detailed user pages showing bytes, warnings, squads, and activity
- **Bytes Economy Configuration**: Set starting balances, daily earnings, cooldowns, and limits per server
- **Role Reward Setup**: Configure which Discord roles users earn based on bytes received
- **Moderation Dashboard**: View all warnings and moderation cases across servers
- **API Key Management**: Generate and revoke API keys for bot authentication
- **Squad Administration**: Create, edit, and delete squads with role assignments
- **File Extension Controls**: Configure which file types are allowed/blocked per server

#### **REST API** (`website/api_routes.py`)
A complete RESTful API that powers both the Discord bot and web interface.

**How it Works:**
- **JWT Authentication**: Secure token-based authentication for all endpoints
- **CRUD Operations**: Full create, read, update, delete operations for all entities
- **Batch Endpoints**: Efficient bulk operations for large datasets
- **Rate Limiting**: API-level rate limiting prevents abuse
- **Query Filtering**: Advanced filtering and pagination for large datasets
- **Data Validation**: Comprehensive input validation and error handling
- **Automatic Documentation**: API endpoints are self-documenting through code structure

### 3. Database Models (`website/models.py`)

#### **Core Entities**
The foundation of the platform's data structure:

- **DiscordUser**: User profiles with Discord ID, username, avatar, and current bytes balance
- **Guild**: Discord server information including name, icon, join date, and moderator role
- **GuildMember**: Join table linking users to servers with activity tracking (last active day, streak count, last daily bytes)
- **Bytes**: Transaction records for all bytes transfers with giver, receiver, amount, reason, and timestamp
- **BytesConfig**: Per-guild economy settings (starting balance, daily earnings, max give amount, cooldowns)
- **BytesRole**: Role rewards configuration linking Discord roles to bytes received thresholds

#### **Moderation System**
Comprehensive moderation tracking and automation:

- **UserWarning**: Warning records with moderator, reason, and timestamp
- **ModerationCase**: Full moderation action logs (bans, kicks, timeouts) with case numbers and resolution tracking
- **AutoModRegexRule**: Username filtering rules with regex patterns, account age limits, and avatar requirements
- **AutoModRateLimit**: Message rate limiting rules for spam detection (message count, duplicates, channel spam)
- **FileAttachment**: File upload tracking with extension, URL, and moderation status

#### **Squad System**
Team organization and management:

- **Squad**: Team definitions with Discord role binding, name, description, and active status
- **SquadMember**: Squad membership tracking with join timestamps

#### **Tracking & Analytics**
Platform usage and performance monitoring:

- **PageView**: Web traffic analytics with path, method, IP, user agent, response time, and bot detection
- **RedirectClick**: Short URL usage tracking with click timestamps and referrer data
- **CommandUsage**: Bot command statistics per user and guild
- **BotStatus**: Bot health monitoring with heartbeat timestamps

### 4. Advanced Features

#### **Smart Caching System**
Multi-layered caching to optimize performance and reduce database load.

**How it Works:**
- **5-Minute TTL**: All cached data expires after 5 minutes to ensure freshness
- **Layered Caching**: Separate caches for user data, guild configurations, and eligibility checks
- **Cache Keys**: Tuple-based keys for complex relationships (user_id, guild_id)
- **Automatic Cleanup**: Old cache entries automatically removed on access
- **Fresh Data Override**: Critical operations can bypass cache when needed

#### **Activity Tracking**
Sophisticated user engagement tracking with programming-themed rewards.

**How it Works:**
- **Daily Activity Detection**: Tracks user's first message each day (UTC) per server
- **Streak Calculation**: Compares last active day with current day and yesterday
- **Multiplier Logic**: Programming data type themed bonuses (8=CHAR, 16=SHORT, 32=INT, 64=LONG)
- **Duplicate Prevention**: Multiple safeguards prevent same-day rewards
- **Cross-Guild Tracking**: Separate streaks maintained per Discord server

#### **File Management**
Automated file attachment monitoring and filtering.

**How it Works:**
- **Extension Rules**: Server admins configure allowed/blocked file extensions
- **Real-time Monitoring**: Bot monitors all message attachments automatically
- **Rule Enforcement**: Messages with blocked extensions are automatically deleted
- **Audit Trail**: All file attachments logged with user, timestamp, and action taken
- **Warning System**: Custom warning messages for blocked file types

#### **Migration System**
Database schema versioning and safe data transformations.

**How it Works:**
- **Alembic Integration**: Uses Alembic for database migrations
- **Version Control**: Each migration has a unique identifier and dependency chain
- **Safe Transformations**: Migrations can be run and rolled back safely
- **Data Migration Scripts**: Separate scripts for complex data transformations
- **Environment Isolation**: Different migration paths for development and production

### 5. Security Features

#### **Authentication & Authorization**
Multi-layered security system protecting both web and API access.

**How it Works:**
- **Admin Web Authentication**: bcrypt-hashed passwords with secure session management
- **API Key Authentication**: Generated API keys for bot-to-server communication
- **JWT Token System**: Short-lived tokens with automatic refresh for API access
- **Role-based Access**: Different permission levels for admin users and API clients
- **Session Security**: Secure session cookies with proper expiration

#### **Rate Limiting & Abuse Prevention**
Comprehensive protection against various forms of abuse.

**How it Works:**
- **API Rate Limiting**: Prevents excessive API requests from overwhelming the server
- **Message Rate Limiting**: Auto-moderation detects and responds to message spam
- **Bytes Cooldowns**: Configurable cooldowns prevent bytes transfer abuse
- **Squad Switch Costs**: Economic barriers prevent rapid squad switching
- **IP-based Tracking**: Monitor requests by IP address for additional protection

#### **Auto-moderation Security**
Proactive threat detection and response system.

**How it Works:**
- **Real-time Monitoring**: Every message and user join event is monitored
- **Pattern Detection**: Regex-based detection of problematic usernames and content
- **Behavioral Analysis**: Tracks user patterns across time for spam detection
- **Immediate Response**: Automatic actions taken within seconds of violation detection
- **Audit Logging**: All moderation actions logged for review and appeals

### 6. Integration Features

#### **Discord Integration**
Deep integration with Discord's API for seamless operation.

**How it Works:**
- **Event-driven Architecture**: Responds immediately to Discord events (joins, messages, updates)
- **Privileged Intents**: Uses GUILD_MEMBERS and GUILD_PRESENCES for advanced features
- **Bulk Operations**: Efficiently handles servers with thousands of members
- **Real-time Sync**: Discord data changes reflected in database immediately
- **Interactive Components**: Uses Discord's buttons and embeds for rich user interfaces

#### **Web Dashboard Integration**
Seamless connection between Discord bot and web administration.

**How it Works:**
- **Live Data Sync**: Web dashboard shows real-time Discord data
- **Bidirectional Control**: Changes in web interface immediately affect bot behavior
- **Unified Analytics**: Combined metrics from both Discord bot and web traffic
- **Configuration Management**: All bot settings configurable through web interface
- **Role Synchronization**: Discord roles automatically managed based on web-configured rules

## System Architecture

**Data Flow:**
1. **Discord Events** → Bot receives events → API calls → Database updates
2. **Web Interface** → Admin makes changes → Database updates → Bot behavior changes
3. **User Commands** → Bot processes → Database transactions → Discord responses
4. **Analytics** → Both bot and web generate metrics → Unified reporting

**Key Integrations:**
- **FastAPI + Starlette**: Modern async web framework for the web application
- **Hikari + Lightbulb**: High-performance Discord bot framework
- **SQLAlchemy**: ORM for database operations with migration support
- **JWT Authentication**: Secure API communication between components
- **Jinja2 Templates**: Dynamic web page generation with admin interfaces

This comprehensive system provides a complete solution for Discord community management, combining engagement mechanics (bytes/squads), moderation tools, and administrative features all integrated between Discord and a web dashboard. The platform is designed to scale efficiently while maintaining security and providing rich analytics for community administrators.