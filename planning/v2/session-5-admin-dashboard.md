# Session 5: Admin Dashboard Pages

## Objective
Create the admin dashboard pages using the Tabler theme. Implement real-time Discord data fetching, configuration management, and analytics displays.

## Prerequisites
- Completed Session 4 (web core structure exists)
- Authentication system working
- Understanding of Discord API integration

## Task 1: Discord Client

### web/services/discord_client.py

Create a Discord API client for fetching data:

```python
import httpx
from typing import Optional, List, Dict, Any
from datetime import datetime
import structlog
from shared.utils import ttl_cache

logger = structlog.get_logger()

class DiscordClient:
    """Client for Discord API interactions."""
    
    def __init__(self, bot_token: str):
        self.token = bot_token
        self.base_url = "https://discord.com/api/v10"
        self.headers = {
            "Authorization": f"Bot {bot_token}",
            "User-Agent": "SmarterDev/2.0"
        }
    
    @ttl_cache(ttl=300)  # Cache for 5 minutes
    async def get_guild(self, guild_id: str) -> Optional[Dict[str, Any]]:
        """Fetch guild information."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/guilds/{guild_id}",
                    headers=self.headers
                )
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    return None
                else:
                    logger.error(
                        "Failed to fetch guild",
                        guild_id=guild_id,
                        status=response.status_code
                    )
                    return None
            except Exception as e:
                logger.error("Discord API error", error=str(e))
                return None
    
    @ttl_cache(ttl=300)
    async def get_guild_member_count(self, guild_id: str) -> int:
        """Get approximate member count for a guild."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/guilds/{guild_id}?with_counts=true",
                    headers=self.headers
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return data.get("approximate_member_count", 0)
                return 0
            except Exception:
                return 0
    
    @ttl_cache(ttl=300)
    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Fetch user information."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/users/{user_id}",
                    headers=self.headers
                )
                
                if response.status_code == 200:
                    return response.json()
                return None
            except Exception:
                return None
    
    async def get_guild_roles(self, guild_id: str) -> List[Dict[str, Any]]:
        """Get all roles in a guild."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/guilds/{guild_id}/roles",
                    headers=self.headers
                )
                
                if response.status_code == 200:
                    return response.json()
                return []
            except Exception:
                return []
    
    async def get_bot_guilds(self) -> List[Dict[str, Any]]:
        """Get all guilds the bot is in."""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/users/@me/guilds",
                    headers=self.headers
                )
                
                if response.status_code == 200:
                    return response.json()
                return []
            except Exception as e:
                logger.error("Failed to fetch bot guilds", error=str(e))
                return []
    
    def get_avatar_url(self, user_id: str, avatar_hash: str, size: int = 128) -> str:
        """Get user avatar URL."""
        if avatar_hash:
            ext = "gif" if avatar_hash.startswith("a_") else "png"
            return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size={size}"
        else:
            # Default avatar
            discriminator = 0  # For new username system
            return f"https://cdn.discordapp.com/embed/avatars/{discriminator % 5}.png"
    
    def get_guild_icon_url(self, guild_id: str, icon_hash: str, size: int = 128) -> str:
        """Get guild icon URL."""
        if icon_hash:
            ext = "gif" if icon_hash.startswith("a_") else "png"
            return f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{ext}?size={size}"
        return None
```

## Task 2: Admin Dashboard Pages

### web/pages/admin.py

Create admin page handlers:

```python
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from sqlalchemy import select, func
from datetime import datetime, timedelta
import structlog

from web.database import get_db
from web.models.bytes import BytesBalance, BytesTransaction, BytesConfig
from web.models.squads import Squad, SquadMembership
from web.models.moderation import ModerationCase
from web.crud.bytes import bytes_crud, config_crud
from shared.utils import utcnow

logger = structlog.get_logger()
templates = Jinja2Templates(directory="web/templates")

async def dashboard(request: Request) -> HTMLResponse:
    """Main admin dashboard."""
    discord = request.app.state.discord_client
    
    # Get statistics
    async for db in get_db():
        # Guild count
        guilds = await discord.get_bot_guilds()
        guild_count = len(guilds)
        
        # User count (unique users with balances)
        user_count_result = await db.execute(
            select(func.count(func.distinct(BytesBalance.user_id)))
        )
        user_count = user_count_result.scalar() or 0
        
        # Recent transactions
        recent_transactions = await db.execute(
            select(BytesTransaction)
            .order_by(BytesTransaction.created_at.desc())
            .limit(10)
        )
        transactions = recent_transactions.scalars().all()
        
        # Activity graph data (last 7 days)
        week_ago = utcnow() - timedelta(days=7)
        daily_stats = await db.execute(
            select(
                func.date(BytesTransaction.created_at).label("date"),
                func.count(BytesTransaction.id).label("count")
            )
            .where(BytesTransaction.created_at >= week_ago)
            .group_by(func.date(BytesTransaction.created_at))
            .order_by(func.date(BytesTransaction.created_at))
        )
        
        activity_data = [
            {"date": str(row.date), "count": row.count}
            for row in daily_stats
        ]
    
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "guild_count": guild_count,
            "user_count": user_count,
            "transactions": transactions,
            "activity_data": activity_data
        }
    )

async def guilds_list(request: Request) -> HTMLResponse:
    """List all guilds with the bot."""
    discord = request.app.state.discord_client
    
    # Get all guilds from Discord
    guilds = await discord.get_bot_guilds()
    
    # Get configuration status for each guild
    async for db in get_db():
        configs = await db.execute(select(BytesConfig))
        config_guild_ids = {c.guild_id for c in configs.scalars()}
    
    # Enhance guild data
    guild_data = []
    for guild in guilds:
        guild_data.append({
            "id": guild["id"],
            "name": guild["name"],
            "icon_url": discord.get_guild_icon_url(guild["id"], guild.get("icon")),
            "is_configured": guild["id"] in config_guild_ids,
            "permissions": guild.get("permissions", 0)
        })
    
    # Sort by name
    guild_data.sort(key=lambda g: g["name"].lower())
    
    return templates.TemplateResponse(
        "admin/guilds/list.html",
        {
            "request": request,
            "guilds": guild_data
        }
    )

async def guild_detail(request: Request) -> HTMLResponse:
    """Guild detail page with tabs."""
    guild_id = request.path_params["guild_id"]
    discord = request.app.state.discord_client
    
    # Fetch guild from Discord
    guild = await discord.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    
    # Get member count
    member_count = await discord.get_guild_member_count(guild_id)
    
    # Get statistics
    async for db in get_db():
        # User count in this guild
        user_count_result = await db.execute(
            select(func.count(BytesBalance.user_id))
            .where(BytesBalance.guild_id == guild_id)
        )
        user_count = user_count_result.scalar() or 0
        
        # Total bytes in circulation
        total_bytes_result = await db.execute(
            select(func.sum(BytesBalance.balance))
            .where(BytesBalance.guild_id == guild_id)
        )
        total_bytes = total_bytes_result.scalar() or 0
        
        # Active squads
        squad_count_result = await db.execute(
            select(func.count(Squad.id))
            .where(Squad.guild_id == guild_id, Squad.is_active == True)
        )
        squad_count = squad_count_result.scalar() or 0
        
        # Recent activity
        recent_activity = await db.execute(
            select(BytesTransaction)
            .where(BytesTransaction.guild_id == guild_id)
            .order_by(BytesTransaction.created_at.desc())
            .limit(5)
        )
        recent_transactions = recent_activity.scalars().all()
    
    # Enhance guild data
    guild_data = {
        "id": guild["id"],
        "name": guild["name"],
        "icon_url": discord.get_guild_icon_url(guild["id"], guild.get("icon")),
        "member_count": member_count,
        "user_count": user_count,
        "total_bytes": total_bytes,
        "squad_count": squad_count,
        "created_at": datetime.fromtimestamp(
            ((int(guild["id"]) >> 22) + 1420070400000) / 1000
        )
    }
    
    # Determine active tab
    tab = request.query_params.get("tab", "overview")
    
    return templates.TemplateResponse(
        "admin/guilds/detail.html",
        {
            "request": request,
            "guild": guild_data,
            "recent_transactions": recent_transactions,
            "active_tab": tab
        }
    )

async def bytes_config(request: Request) -> HTMLResponse:
    """Bytes configuration page."""
    guild_id = request.path_params["guild_id"]
    discord = request.app.state.discord_client
    
    # Verify guild exists
    guild = await discord.get_guild(guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")
    
    async for db in get_db():
        if request.method == "GET":
            # Get current config
            config = await config_crud.get(db, guild_id=guild_id)
            
            # Get guild roles for role rewards
            roles = await discord.get_guild_roles(guild_id)
            
            return templates.TemplateResponse(
                "admin/guilds/tabs/bytes.html",
                {
                    "request": request,
                    "guild": guild,
                    "config": config,
                    "roles": roles
                }
            )
        
        elif request.method == "POST":
            # Update configuration
            form = await request.form()
            
            config = await config_crud.get(db, guild_id=guild_id)
            if not config:
                config = await config_crud.create(db, guild_id=guild_id)
            
            # Update values
            await config_crud.update(
                db,
                config,
                starting_balance=int(form.get("starting_balance", 100)),
                daily_amount=int(form.get("daily_amount", 10)),
                max_transfer=int(form.get("max_transfer", 1000)),
                cooldown_hours=int(form.get("cooldown_hours", 24))
            )
            
            # Handle role rewards
            role_rewards = {}
            for key, value in form.items():
                if key.startswith("role_") and value:
                    role_id = key.replace("role_", "")
                    role_rewards[role_id] = int(value)
            
            await config_crud.update(db, config, role_rewards=role_rewards)
            
            # Notify bot via Redis
            redis = request.app.state.redis
            await redis.publish(
                f"config_update:{guild_id}",
                "bytes"
            )
            
            return RedirectResponse(
                url=f"/admin/guilds/{guild_id}?tab=bytes",
                status_code=303
            )

async def user_detail(request: Request) -> HTMLResponse:
    """User detail page."""
    user_id = request.path_params["user_id"]
    discord = request.app.state.discord_client
    
    # Fetch user from Discord
    user = await discord.get_user(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    
    # Get user data across all guilds
    async for db in get_db():
        # Bytes balances
        balances = await db.execute(
            select(BytesBalance)
            .where(BytesBalance.user_id == user_id)
            .order_by(BytesBalance.balance.desc())
        )
        user_balances = balances.scalars().all()
        
        # Recent transactions
        transactions = await db.execute(
            select(BytesTransaction)
            .where(
                (BytesTransaction.giver_id == user_id) |
                (BytesTransaction.receiver_id == user_id)
            )
            .order_by(BytesTransaction.created_at.desc())
            .limit(20)
        )
        user_transactions = transactions.scalars().all()
        
        # Squad memberships
        memberships = await db.execute(
            select(SquadMembership, Squad)
            .join(Squad)
            .where(SquadMembership.user_id == user_id)
        )
        user_squads = [
            {"guild_id": m.guild_id, "squad": s}
            for m, s in memberships
        ]
        
        # Moderation cases
        cases = await db.execute(
            select(ModerationCase)
            .where(ModerationCase.user_id == user_id)
            .order_by(ModerationCase.created_at.desc())
        )
        user_cases = cases.scalars().all()
    
    # Enhance user data
    user_data = {
        "id": user["id"],
        "username": user["username"],
        "discriminator": user.get("discriminator", "0"),
        "avatar_url": discord.get_avatar_url(user["id"], user.get("avatar")),
        "created_at": datetime.fromtimestamp(
            ((int(user["id"]) >> 22) + 1420070400000) / 1000
        ),
        "balances": user_balances,
        "transactions": user_transactions,
        "squads": user_squads,
        "moderation_cases": user_cases
    }
    
    return templates.TemplateResponse(
        "admin/users/detail.html",
        {
            "request": request,
            "user": user_data
        }
    )

async def settings(request: Request) -> HTMLResponse:
    """Admin settings page."""
    if request.method == "GET":
        # Get API keys
        async for db in get_db():
            from web.models.admin import APIKey
            keys = await db.execute(
                select(APIKey)
                .where(APIKey.is_active == True)
                .order_by(APIKey.created_at.desc())
            )
            api_keys = keys.scalars().all()
        
        return templates.TemplateResponse(
            "admin/settings.html",
            {
                "request": request,
                "api_keys": api_keys
            }
        )
    
    elif request.method == "POST":
        # Handle settings updates
        form = await request.form()
        action = form.get("action")
        
        if action == "create_api_key":
            # Create new API key
            from shared.utils import generate_token
            from web.models.admin import APIKey
            
            key = generate_token(32)
            key_hash = APIKey.hash_key(key)
            name = form.get("name", "Unnamed Key")
            
            async for db in get_db():
                api_key = APIKey(
                    key_hash=key_hash,
                    name=name
                )
                db.add(api_key)
                await db.commit()
            
            # Show the key once (won't be shown again)
            return templates.TemplateResponse(
                "admin/settings.html",
                {
                    "request": request,
                    "new_api_key": key,
                    "api_key_name": name
                }
            )
        
        return RedirectResponse(url="/admin/settings", status_code=303)
```

## Task 3: Admin Templates

### web/templates/admin/dashboard.html

Main dashboard template:

```html
{% extends "base.html" %}

{% block title %}Dashboard - Smarter Dev Admin{% endblock %}

{% block content %}
<div class="container-xl">
    <!-- Page header -->
    <div class="page-header d-print-none">
        <div class="container-xl">
            <div class="row g-2 align-items-center">
                <div class="col">
                    <h2 class="page-title">Dashboard</h2>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Page body -->
    <div class="page-body">
        <div class="container-xl">
            <!-- Stats cards -->
            <div class="row row-deck row-cards">
                <div class="col-sm-6 col-lg-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="subheader">Total Guilds</div>
                            </div>
                            <div class="h1 mb-0">{{ guild_count }}</div>
                        </div>
                    </div>
                </div>
                
                <div class="col-sm-6 col-lg-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="subheader">Active Users</div>
                            </div>
                            <div class="h1 mb-0">{{ user_count }}</div>
                        </div>
                    </div>
                </div>
                
                <div class="col-sm-6 col-lg-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="subheader">Transactions Today</div>
                            </div>
                            <div class="h1 mb-0">{{ transactions|length }}</div>
                        </div>
                    </div>
                </div>
                
                <div class="col-sm-6 col-lg-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="subheader">Bot Status</div>
                            </div>
                            <div class="h1 mb-0 text-green">Online</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Activity chart -->
            <div class="row row-deck row-cards mt-3">
                <div class="col-12">
                    <div class="card">
                        <div class="card-header">
                            <h3 class="card-title">Activity (Last 7 Days)</h3>
                        </div>
                        <div class="card-body">
                            <div id="activity-chart" style="height: 300px"></div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Recent transactions -->
            <div class="row row-deck row-cards mt-3">
                <div class="col-12">
                    <div class="card">
                        <div class="card-header">
                            <h3 class="card-title">Recent Transactions</h3>
                        </div>
                        <div class="table-responsive">
                            <table class="table table-vcenter card-table">
                                <thead>
                                    <tr>
                                        <th>Time</th>
                                        <th>From</th>
                                        <th>To</th>
                                        <th>Amount</th>
                                        <th>Reason</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for tx in transactions %}
                                    <tr>
                                        <td class="text-muted">
                                            {{ tx.created_at.strftime('%Y-%m-%d %H:%M') }}
                                        </td>
                                        <td>{{ tx.giver_username }}</td>
                                        <td>{{ tx.receiver_username }}</td>
                                        <td>{{ tx.amount }} bytes</td>
                                        <td>{{ tx.reason or "-" }}</td>
                                    </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<script>
document.addEventListener('DOMContentLoaded', function() {
    // Activity chart
    const activityData = {{ activity_data|tojson }};
    
    const options = {
        series: [{
            name: 'Transactions',
            data: activityData.map(d => d.count)
        }],
        chart: {
            type: 'area',
            height: 300,
            sparkline: {
                enabled: true
            }
        },
        stroke: {
            curve: 'smooth',
            width: 2
        },
        fill: {
            opacity: 0.3
        },
        xaxis: {
            categories: activityData.map(d => d.date)
        },
        colors: ['#3b82f6']
    };
    
    const chart = new ApexCharts(document.querySelector("#activity-chart"), options);
    chart.render();
});
</script>
{% endblock %}
```

### web/templates/admin/guilds/detail.html

Guild detail page with tabs:

```html
{% extends "base.html" %}

{% block title %}{{ guild.name }} - Smarter Dev Admin{% endblock %}

{% block content %}
<div class="container-xl">
    <!-- Page header -->
    <div class="page-header d-print-none">
        <div class="container-xl">
            <div class="row g-2 align-items-center">
                <div class="col">
                    <div class="page-pretitle">
                        <a href="/admin/guilds">Guilds</a>
                    </div>
                    <h2 class="page-title">
                        {% if guild.icon_url %}
                        <span class="avatar avatar-sm me-2" 
                              style="background-image: url({{ guild.icon_url }})"></span>
                        {% endif %}
                        {{ guild.name }}
                    </h2>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Guild info cards -->
    <div class="page-body">
        <div class="container-xl">
            <div class="row row-cards mb-3">
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="subheader">Members</div>
                            <div class="h3 mb-0">{{ guild.member_count }}</div>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="subheader">Active Users</div>
                            <div class="h3 mb-0">{{ guild.user_count }}</div>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="subheader">Total Bytes</div>
                            <div class="h3 mb-0">{{ guild.total_bytes }}</div>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card">
                        <div class="card-body">
                            <div class="subheader">Active Squads</div>
                            <div class="h3 mb-0">{{ guild.squad_count }}</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Tabs -->
            <div class="card">
                <div class="card-header">
                    <ul class="nav nav-tabs card-header-tabs" data-bs-toggle="tabs">
                        <li class="nav-item">
                            <a href="?tab=overview" class="nav-link {% if active_tab == 'overview' %}active{% endif %}">
                                Overview
                            </a>
                        </li>
                        <li class="nav-item">
                            <a href="?tab=bytes" class="nav-link {% if active_tab == 'bytes' %}active{% endif %}">
                                Bytes Config
                            </a>
                        </li>
                        <li class="nav-item">
                            <a href="?tab=squads" class="nav-link {% if active_tab == 'squads' %}active{% endif %}">
                                Squads
                            </a>
                        </li>
                        <li class="nav-item">
                            <a href="?tab=automod" class="nav-link {% if active_tab == 'automod' %}active{% endif %}">
                                Auto-Moderation
                            </a>
                        </li>
                    </ul>
                </div>
                <div class="card-body">
                    <div class="tab-content">
                        {% if active_tab == 'overview' %}
                            {% include 'admin/guilds/tabs/overview.html' %}
                        {% elif active_tab == 'bytes' %}
                            {% include 'admin/guilds/tabs/bytes.html' %}
                        {% elif active_tab == 'squads' %}
                            {% include 'admin/guilds/tabs/squads.html' %}
                        {% elif active_tab == 'automod' %}
                            {% include 'admin/guilds/tabs/automod.html' %}
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
```

### web/templates/admin/guilds/tabs/bytes.html

Bytes configuration tab:

```html
<form method="post">
    <div class="row">
        <div class="col-md-6">
            <div class="mb-3">
                <label class="form-label">Starting Balance</label>
                <input type="number" name="starting_balance" class="form-control" 
                       value="{{ config.starting_balance if config else 100 }}" min="0">
                <small class="form-hint">Bytes given to new users</small>
            </div>
            
            <div class="mb-3">
                <label class="form-label">Daily Amount</label>
                <input type="number" name="daily_amount" class="form-control"
                       value="{{ config.daily_amount if config else 10 }}" min="0">
                <small class="form-hint">Base daily reward (before multipliers)</small>
            </div>
        </div>
        
        <div class="col-md-6">
            <div class="mb-3">
                <label class="form-label">Max Transfer</label>
                <input type="number" name="max_transfer" class="form-control"
                       value="{{ config.max_transfer if config else 1000 }}" min="1">
                <small class="form-hint">Maximum bytes per transfer</small>
            </div>
            
            <div class="mb-3">
                <label class="form-label">Cooldown (hours)</label>
                <input type="number" name="cooldown_hours" class="form-control"
                       value="{{ config.cooldown_hours if config else 24 }}" min="0">
                <small class="form-hint">Hours between transfers</small>
            </div>
        </div>
    </div>
    
    <h3 class="mt-4">Role Rewards</h3>
    <p class="text-muted">Users automatically receive roles when they reach these byte thresholds.</p>
    
    <div class="row">
        {% for role in roles %}
        {% if not role.managed and role.name != "@everyone" %}
        <div class="col-md-6">
            <div class="mb-3">
                <label class="form-label">
                    <span class="badge" style="background-color: #{{ '%06x' % role.color }}">
                        {{ role.name }}
                    </span>
                </label>
                <input type="number" name="role_{{ role.id }}" class="form-control"
                       value="{{ config.role_rewards.get(role.id) if config else '' }}"
                       placeholder="Bytes required (leave empty to disable)">
            </div>
        </div>
        {% endif %}
        {% endfor %}
    </div>
    
    <div class="mt-4">
        <button type="submit" class="btn btn-primary">Save Configuration</button>
    </div>
</form>
```

## Task 4: JavaScript for Admin

### web/static/js/admin.js

Enhanced admin JavaScript:

```javascript
// Admin-specific functionality
class AdminPanel {
    constructor() {
        this.initializeGuildSearch();
        this.initializeConfirmations();
        this.initializeTabPersistence();
    }
    
    initializeGuildSearch() {
        const searchInput = document.getElementById('guild-search');
        if (!searchInput) return;
        
        searchInput.addEventListener('input', (e) => {
            const query = e.target.value.toLowerCase();
            const guildCards = document.querySelectorAll('.guild-card');
            
            guildCards.forEach(card => {
                const name = card.dataset.guildName.toLowerCase();
                card.style.display = name.includes(query) ? '' : 'none';
            });
        });
    }
    
    initializeConfirmations() {
        // Confirm before dangerous actions
        document.querySelectorAll('[data-confirm]').forEach(element => {
            element.addEventListener('click', (e) => {
                const message = element.dataset.confirm;
                if (!confirm(message)) {
                    e.preventDefault();
                }
            });
        });
    }
    
    initializeTabPersistence() {
        // Remember active tab
        const tabs = document.querySelectorAll('[data-bs-toggle="tabs"] .nav-link');
        tabs.forEach(tab => {
            tab.addEventListener('shown.bs.tab', (e) => {
                const url = new URL(window.location);
                url.searchParams.set('tab', e.target.href.split('tab=')[1]);
                window.history.replaceState({}, '', url);
            });
        });
    }
    
    async refreshGuildData(guildId) {
        try {
            const response = await fetch(`/api/v1/guilds/${guildId}/refresh`, {
                method: 'POST'
            });
            
            if (response.ok) {
                window.location.reload();
            }
        } catch (error) {
            console.error('Failed to refresh guild data:', error);
        }
    }
}

// Initialize when ready
document.addEventListener('DOMContentLoaded', () => {
    window.adminPanel = new AdminPanel();
});
```

## Task 5: Create Tests

### tests/test_admin_pages.py

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_admin_requires_auth(test_client: AsyncClient):
    """Test admin pages require authentication."""
    response = await test_client.get("/admin")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["location"]

@pytest.mark.asyncio
async def test_admin_dashboard(auth_client: AsyncClient):
    """Test admin dashboard loads."""
    response = await auth_client.get("/admin")
    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert "Total Guilds" in response.text

@pytest.mark.asyncio
async def test_guilds_list(auth_client: AsyncClient, mock_discord):
    """Test guilds list page."""
    # Mock Discord API response
    mock_discord.get_bot_guilds.return_value = [
        {"id": "123", "name": "Test Guild", "icon": None}
    ]
    
    response = await auth_client.get("/admin/guilds")
    assert response.status_code == 200
    assert "Test Guild" in response.text

@pytest.mark.asyncio
async def test_guild_detail(auth_client: AsyncClient, mock_discord):
    """Test guild detail page."""
    # Mock Discord API responses
    mock_discord.get_guild.return_value = {
        "id": "123",
        "name": "Test Guild",
        "icon": None
    }
    mock_discord.get_guild_member_count.return_value = 100
    
    response = await auth_client.get("/admin/guilds/123")
    assert response.status_code == 200
    assert "Test Guild" in response.text
    assert "100" in response.text  # Member count

@pytest.mark.asyncio
async def test_bytes_config_update(auth_client: AsyncClient, test_db):
    """Test updating bytes configuration."""
    response = await auth_client.post(
        "/admin/guilds/123/bytes",
        data={
            "starting_balance": "200",
            "daily_amount": "20",
            "max_transfer": "2000",
            "cooldown_hours": "12"
        }
    )
    
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/guilds/123?tab=bytes"
    
    # Verify config was saved
    from web.crud.bytes import config_crud
    config = await config_crud.get(test_db, guild_id="123")
    assert config.starting_balance == 200
    assert config.daily_amount == 20
```

## Deliverables

1. **Discord Client**
   - API wrapper with caching
   - User/guild data fetching
   - Avatar/icon URL helpers

2. **Admin Pages**
   - Dashboard with statistics
   - Guild list and search
   - Guild detail with tabs
   - User lookup
   - Settings management

3. **Configuration UI**
   - Bytes economy settings
   - Role reward configuration
   - Real-time updates via Redis

4. **Templates**
   - Professional Tabler theme
   - Responsive design
   - Interactive tabs
   - Loading states

5. **JavaScript**
   - Guild search
   - Tab persistence
   - Confirmation dialogs
   - AJAX updates

6. **Test Coverage**
   - Authentication tests
   - Page rendering tests
   - Form submission tests

## Important Notes

1. Always fetch Discord data on-demand
2. Cache Discord API responses for 5 minutes
3. Use Redis pub/sub for config updates
4. Show loading states for API calls
5. Handle Discord API errors gracefully
6. Responsive design for mobile admins

This admin dashboard provides comprehensive guild management with a professional interface.