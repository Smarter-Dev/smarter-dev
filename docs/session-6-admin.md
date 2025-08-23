# Session 6: Admin Interface Implementation

**Goal:** Create authenticated admin interface for guild configuration

## Task Description

Create web admin interface for configuring bytes and squads per guild.

### Requirements
1. Integrate with existing Starlette app
2. Use session-based authentication
3. Fetch Discord data on-demand (no duplication)
4. Clean UI with proper error handling
5. Test admin routes and authentication

## Deliverables

### 1. web/admin/auth.py - Authentication for admin:
```python
from starlette.authentication import requires
from starlette.responses import RedirectResponse
from functools import wraps

def admin_required(func):
    """Decorator to require admin authentication"""
    @wraps(func)
    async def wrapper(request):
        # Check if user is authenticated
        if not request.user.is_authenticated:
            return RedirectResponse(url="/admin/login", status_code=303)
        
        # In production, check Discord OAuth
        # In dev mode, just check session
        if not request.session.get("is_admin"):
            return RedirectResponse(url="/admin/login", status_code=303)
        
        return await func(request)
    return wrapper

async def login(request):
    """Admin login page"""
    if request.method == "GET":
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request}
        )
    
    # POST - Dev mode only
    form = await request.form()
    username = form.get("username")
    
    if username and len(username) >= 3:
        request.session["user_id"] = username
        request.session["is_admin"] = True
        
        # Redirect to requested page or dashboard
        next_url = request.query_params.get("next", "/admin")
        return RedirectResponse(url=next_url, status_code=303)
    
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "error": "Invalid username"}
    )

async def logout(request):
    """Admin logout"""
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
```

### 2. web/admin/routes.py - Admin routes:
```python
from starlette.routing import Route, Mount
from web.admin.views import *
from web.admin.auth import admin_required, login, logout

admin_routes = [
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/", admin_required(dashboard), name="admin_dashboard"),
    Route("/guilds", admin_required(guild_list), name="admin_guilds"),
    Route("/guilds/{guild_id}", admin_required(guild_detail), name="admin_guild_detail"),
    Route("/guilds/{guild_id}/bytes", admin_required(bytes_config), methods=["GET", "POST"]),
    Route("/guilds/{guild_id}/squads", admin_required(squads_config), methods=["GET", "POST"]),
]

# Mount to main app
app.mount("/admin", Mount(routes=admin_routes))
```

### 3. web/admin/views.py - Admin view handlers:
```python
from web.admin.discord import get_bot_guilds, get_guild_info
from web.crud import BytesOperations, SquadOperations

async def dashboard(request):
    """Admin dashboard with overview"""
    # Get bot guilds from Discord
    guilds = await get_bot_guilds()
    
    # Get stats from database
    async with get_db_session() as session:
        total_users = await session.execute(
            select(func.count(distinct(BytesBalance.user_id)))
        )
        total_transactions = await session.execute(
            select(func.count(BytesTransaction.id))
        )
    
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "guilds": guilds,
            "total_users": total_users.scalar(),
            "total_transactions": total_transactions.scalar()
        }
    )

async def guild_detail(request):
    """Guild detail page"""
    guild_id = request.path_params["guild_id"]
    
    # Fetch guild info from Discord
    try:
        guild = await get_guild_info(guild_id)
    except GuildNotFoundError:
        return templates.TemplateResponse(
            "admin/error.html",
            {"request": request, "error": "Guild not found"},
            status_code=404
        )
    
    # Get guild stats
    async with get_db_session() as session:
        ops = BytesOperations()
        
        # Get top users
        top_users = await ops.get_leaderboard(session, guild_id, limit=5)
        
        # Get recent transactions
        recent_transactions = await session.execute(
            select(BytesTransaction)
            .where(BytesTransaction.guild_id == guild_id)
            .order_by(BytesTransaction.created_at.desc())
            .limit(10)
        )
        
        # Get config
        config = await ops.get_config(session, guild_id)
    
    return templates.TemplateResponse(
        "admin/guild_detail.html",
        {
            "request": request,
            "guild": guild,
            "top_users": top_users,
            "recent_transactions": recent_transactions.scalars().all(),
            "config": config
        }
    )

async def bytes_config(request):
    """Bytes configuration for guild"""
    guild_id = request.path_params["guild_id"]
    
    # Verify guild exists
    try:
        guild = await get_guild_info(guild_id)
    except GuildNotFoundError:
        return templates.TemplateResponse(
            "admin/error.html",
            {"request": request, "error": "Guild not found"},
            status_code=404
        )
    
    async with get_db_session() as session:
        ops = BytesOperations()
        
        if request.method == "GET":
            config = await ops.get_config(session, guild_id)
            
            return templates.TemplateResponse(
                "admin/bytes_config.html",
                {
                    "request": request,
                    "guild": guild,
                    "config": config or BytesConfig.get_defaults(guild_id)
                }
            )
        
        # POST - Update config
        form = await request.form()
        
        config_data = {
            "starting_balance": int(form.get("starting_balance", 100)),
            "daily_amount": int(form.get("daily_amount", 10)),
            "max_transfer": int(form.get("max_transfer", 1000)),
            "transfer_cooldown_hours": int(form.get("transfer_cooldown_hours", 0))
        }
        
        # Parse role rewards
        role_rewards = {}
        for key, value in form.items():
            if key.startswith("role_reward_"):
                role_id = key.replace("role_reward_", "")
                if value:
                    role_rewards[role_id] = int(value)
        
        config_data["role_rewards"] = role_rewards
        
        # Update config
        config = await ops.update_config(session, guild_id, config_data)
        await session.commit()
        
        # Notify bot via Redis
        await redis.publish(
            f"config_update:{guild_id}",
            json.dumps({"type": "bytes", "guild_id": guild_id})
        )
        
        return templates.TemplateResponse(
            "admin/bytes_config.html",
            {
                "request": request,
                "guild": guild,
                "config": config,
                "success": "Configuration updated successfully!"
            }
        )

async def squads_config(request):
    """Squad management for guild"""
    guild_id = request.path_params["guild_id"]
    
    # Verify guild exists
    try:
        guild = await get_guild_info(guild_id)
        guild_roles = await get_guild_roles(guild_id)
    except GuildNotFoundError:
        return templates.TemplateResponse(
            "admin/error.html",
            {"request": request, "error": "Guild not found"},
            status_code=404
        )
    
    async with get_db_session() as session:
        ops = SquadOperations()
        
        if request.method == "GET":
            squads = await ops.list_squads(session, guild_id)
            
            return templates.TemplateResponse(
                "admin/squads_config.html",
                {
                    "request": request,
                    "guild": guild,
                    "guild_roles": guild_roles,
                    "squads": squads
                }
            )
        
        # POST - Handle squad actions
        form = await request.form()
        action = form.get("action")
        
        if action == "create":
            squad_data = {
                "guild_id": guild_id,
                "role_id": form.get("role_id"),
                "name": form.get("name"),
                "description": form.get("description"),
                "switch_cost": int(form.get("switch_cost", 50)),
                "max_members": int(form.get("max_members")) if form.get("max_members") else None
            }
            
            await ops.create_squad(session, squad_data)
            await session.commit()
            
            success = "Squad created successfully!"
        
        elif action == "update":
            squad_id = UUID(form.get("squad_id"))
            updates = {
                "name": form.get("name"),
                "description": form.get("description"),
                "switch_cost": int(form.get("switch_cost")),
                "is_active": form.get("is_active") == "on"
            }
            
            await ops.update_squad(session, squad_id, updates)
            await session.commit()
            
            success = "Squad updated successfully!"
        
        elif action == "delete":
            squad_id = UUID(form.get("squad_id"))
            await ops.delete_squad(session, squad_id)
            await session.commit()
            
            success = "Squad deleted successfully!"
        
        # Refresh squads list
        squads = await ops.list_squads(session, guild_id)
        
        return templates.TemplateResponse(
            "admin/squads_config.html",
            {
                "request": request,
                "guild": guild,
                "guild_roles": guild_roles,
                "squads": squads,
                "success": success
            }
        )
```

### 4. web/admin/discord.py - Discord API helpers:
```python
import httpx
from typing import List, Dict
from shared.config import settings

class DiscordClient:
    def __init__(self, bot_token: str):
        self.headers = {"Authorization": f"Bot {bot_token}"}
        self.base_url = "https://discord.com/api/v10"
    
    async def get_bot_guilds(self) -> List[Dict]:
        """Get all guilds the bot is in"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/users/@me/guilds",
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()
    
    async def get_guild(self, guild_id: str) -> Dict:
        """Get guild information"""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/guilds/{guild_id}",
                headers=self.headers
            )
            
            if response.status_code == 404:
                raise GuildNotFoundError(f"Guild {guild_id} not found")
            
            response.raise_for_status()
            return response.json()
    
    async def get_guild_roles(self, guild_id: str) -> List[Dict]:
        """Get guild roles"""
        guild = await self.get_guild(guild_id)
        return guild.get("roles", [])

# Create singleton
discord_client = DiscordClient(settings.DISCORD_BOT_TOKEN)

async def get_bot_guilds():
    return await discord_client.get_bot_guilds()

async def get_guild_info(guild_id: str):
    return await discord_client.get_guild(guild_id)

async def get_guild_roles(guild_id: str):
    return await discord_client.get_guild_roles(guild_id)
```

### 5. web/templates/admin/base.html - Base admin template:
```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Smarter Dev Admin{% endblock %}</title>
    <link rel="stylesheet" href="https://unpkg.com/@tabler/core@latest/dist/css/tabler.min.css">
</head>
<body>
    <div class="page">
        <header class="navbar navbar-expand-md navbar-dark navbar-overlap d-print-none">
            <div class="container-xl">
                <h1 class="navbar-brand navbar-brand-autodark d-none-navbar-horizontal pe-0 pe-md-3">
                    <a href="/admin">Smarter Dev Admin</a>
                </h1>
                <div class="navbar-nav flex-row order-md-last">
                    <div class="nav-item dropdown">
                        <a href="#" class="nav-link d-flex lh-1 text-reset p-0" data-bs-toggle="dropdown">
                            <span class="avatar avatar-sm">{{ request.session.user_id[:2].upper() }}</span>
                        </a>
                        <div class="dropdown-menu dropdown-menu-end dropdown-menu-arrow">
                            <form action="/admin/logout" method="post">
                                <button type="submit" class="dropdown-item">Logout</button>
                            </form>
                        </div>
                    </div>
                </div>
            </div>
        </header>
        
        <div class="page-wrapper">
            <div class="container-xl">
                {% block content %}{% endblock %}
            </div>
        </div>
    </div>
    
    <script src="https://unpkg.com/@tabler/core@latest/dist/js/tabler.min.js"></script>
    {% block scripts %}{% endblock %}
</body>
</html>
```

### 6. tests/web/test_admin.py - Admin interface tests:
```python
class TestAdminInterface:
    @pytest.fixture
    async def admin_client(self, client):
        """Client with admin session"""
        client.session["user_id"] = "test_admin"
        client.session["is_admin"] = True
        return client
    
    async def test_dashboard_requires_auth(self, client):
        response = await client.get("/admin/")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
    
    async def test_dashboard_with_auth(self, admin_client, mock_discord):
        mock_discord.get_bot_guilds.return_value = [
            {"id": "123", "name": "Test Guild", "icon": None}
        ]
        
        response = await admin_client.get("/admin/")
        assert response.status_code == 200
        assert b"Test Guild" in response.content
    
    async def test_bytes_config_update(self, admin_client, mock_discord):
        mock_discord.get_guild.return_value = {
            "id": "123",
            "name": "Test Guild",
            "icon": None
        }
        
        response = await admin_client.post(
            "/admin/guilds/123/bytes",
            data={
                "starting_balance": "200",
                "daily_amount": "20",
                "max_transfer": "2000",
                "transfer_cooldown_hours": "0"
            }
        )
        
        assert response.status_code == 200
        assert b"Configuration updated successfully!" in response.content
```

## Quality Requirements
All admin pages should:
- Require authentication
- Fetch Discord data on-demand
- Handle errors gracefully
- Provide clear feedback
- Be fully tested