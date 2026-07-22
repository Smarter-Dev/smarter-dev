# Extension System — Implementation Design

Status: IMPLEMENTED. Branch: `feat/bot-core-parity`. This document has been
reconciled with the shipped code; the signatures and file references below match
what was built (see §9 for the deviations forced during implementation).

An **extension** is a first-party, code-reviewed bundle of admin-handler templates
that ships in the repo. An admin browses a catalog in the Skrift admin panel,
picks a guild, fills a config form generated from the extension's declared
schema, and installs. Installing materializes real `admin_handlers` rows for
that guild with the config values baked into the scripts as **string literals**
(the literal rails: `handler_lint._ROLE_ID_LITERAL`, the runtime
`allowed_role_ids` allowlist consumed at `admin_handlers_jobs.py:159`, and the
fire path generally). Installs are tracked so uninstall removes exactly its own
rows, config can be re-edited, and a newer in-repo version surfaces as a
one-click update.

Extensions bypass the conversational authoring/judge pipeline (they are
pre-reviewed), but every model-level constraint and static rail the
admin-handler tables enforce still applies: trigger vocabulary
(`ADMIN_HANDLER_TRIGGER_TYPES`), name length/uniqueness
(`uq_admin_handlers_guild_name`), `include_bot_messages` only on `message`
triggers, `MAX_ADMIN_HANDLERS_PER_GUILD` (= 20, `handler_caps.py`), the
schedule-settings vocabulary (`handler_schedule.py`), and
`handler_lint.lint_script` on every rendered script.

Codebase facts this design is built on (verified on this branch):

- `AdminHandler` (`smarter_dev/web/models.py` ~4465): columns `guild_id`,
  `name` (≤64, unique per guild), `trigger_type` (CHECK against the 12-value
  vocabulary), `settings` JSON, `channel_ids` JSON (empty = all channels),
  `description`, `script`, `created_by_admin` `String(20)`, `enabled`,
  `memory`, `scheduled_job_id`.
- Creation rails live in `smarter_dev/web/api_native/admin_handlers.py`
  (`create_admin_handler`): trigger check, `_reject_bot_optin_on_non_message`,
  `_normalized_name`, `_name_taken`, guild cap, and `_reschedule` (arm the
  first fire via `handler_schedule.first_fire_at` + `skrift.workers.submit`
  with `AdminHandlerFirePayload`) for `schedule`/`timer` triggers. The job is
  armed **before** commit; an orphaned queue job fires to a harmless
  `{"status": "missing"}` (`admin_handlers_jobs.run_admin_handler_fire`).
- Fire time: `settings["allowed_role_ids"]` is the host-enforced role-grant
  allowlist; `handler_lint` rejects non-literal role ids in
  `add_role`/`remove_role` and hardcoded `delete_thread` targets.
- Schedules are the explicit vocabulary in `handler_schedule.py` — NOT cron:
  `schedule` = `{"interval_seconds": int}` or `{"daily_time": "HH:MM"}` (UTC),
  optionally with `{"start_at": ISO-8601 UTC}`; `timer` =
  `{"delay_seconds": int}` or `{"fire_at": ISO}`. For interval schedules,
  `start_at` anchors the recurrence; for daily schedules it is a lower bound.
  Recurrence floors are `MIN_INTERVAL_SECONDS` and
  `MIN_INTERVAL_WITH_AGENT_SECONDS`.
- The bot needs **no push** on install: `plugins/handler_events.py` polls
  `GET /api/handlers/active-channels` on a refresh loop and picks up new
  enabled rows/triggers.
- Skrift admin conventions: controllers registered in `app.yaml`
  `controllers:`; guild-scoped pages live in `smarter_dev/web/bot_admin/*` at
  `/admin/bot/guilds/{guild_id}/...` with per-route
  `guards=[auth_guard, Permission("administrator")]`, `fetch_guild_or_error`
  (from `bot_admin/campaigns.py`), `get_admin_context`, `skrift.flash`
  helpers, raw `await request.form()` URL-encoded parsing with pure
  `read_*_form`/`validate_*_form` helpers, templates under
  `templates/admin/bot/<area>/`, sidebar entries in
  `templates/admin/bot/_sidebar.html` keyed on `active_page`.
- Migrations: `alembic/main`, table ownership registered in `MAIN_TABLES` in
  `alembic/main/env.py` (guarded by `tests/test_migration_ownership.py`);
  pattern file `20260718_120000_c3d5e7f9a1b2_guild_handler_memory.py`. House
  style in this model area: **no ForeignKey constraints** (cf.
  `handler_runs.handler_id`, `guild_handler_memory`) — plain typed columns +
  indexes, integrity owned by the service layer.

Deviations from the product brief, forced by the codebase, are collected in §9.

---

## 1. Extension manifest format and on-disk layout

### 1.1 On-disk layout

```
smarter_dev/extensions/                    # new top-level package (peer of web/, bot/)
    __init__.py
    schema.py          # pydantic manifest models (ConfigField, HandlerTemplate, ExtensionManifest)
    rendering.py       # pure placeholder substitution + validation (§2)
    registry.py        # catalog discovery, validation, ExtensionRegistry (§4)
    catalog/
        __init__.py
        dm_forum_relay/
            __init__.py            # defines MANIFEST: ExtensionManifest
            dm_mirror.monty        # script template, dm_message trigger
            forum_relay.monty      # script template, message trigger scoped to the forum channel
        <next_extension>/
            __init__.py
            *.monty
```

One directory per extension under `smarter_dev/extensions/catalog/`. The
`__init__.py` defines a single module-level constant `MANIFEST` (an
`ExtensionManifest`). Script templates are sibling `*.monty` files loaded with
`(Path(__file__).parent / handler.script_file).read_text()` — they are Monty
source with placeholders, kept out of Python string literals so they diff and
review cleanly.

### 1.2 Manifest models (`smarter_dev/extensions/schema.py`)

Pydantic models, `model_config = ConfigDict(frozen=True)`:

```python
CONFIG_FIELD_TYPES = ("channel_id", "role_id", "string", "int", "bool")


class ConfigField(BaseModel):
    name: str                 # ^[a-z][a-z0-9_]{0,39}$ — the {{cfg.<name>}} key
    type: str                 # one of CONFIG_FIELD_TYPES
    label: str                # form label, e.g. "Relay forum channel"
    help: str = ""            # form help text under the input
    required: bool = True
    default: str | int | bool | None = None   # prefill; type-checked against `type`


class HandlerTemplate(BaseModel):
    key: str                  # ^[a-z][a-z0-9_-]{0,39}$ — stable identity within the
                              # extension; update/config-edit match rows by this key
    name: str                 # the AdminHandler.name to materialize (≤64, static —
                              # no placeholders, so update can rename deterministically)
    trigger_type: str         # must be in models.ADMIN_HANDLER_TRIGGER_TYPES
    description: str          # AdminHandler.description (what the handler does)
    script_file: str          # relative *.monty filename in the extension dir
    settings: dict = {}       # JSON template — string values may be/contain
                              # {{cfg.*}} placeholders (§2.3). Carries
                              # allowed_role_ids, include_bot_messages, and the
                              # schedule/timer spec exactly as the row stores them.
    channel_scope: list[str] = []   # names of channel_id config fields whose values
                              # become AdminHandler.channel_ids; [] = guild-wide


class ExtensionManifest(BaseModel):
    slug: str                 # ^[a-z][a-z0-9-]{0,63}$ — catalog identity, stored on installs
    title: str                # catalog card heading
    summary: str              # 1–3 sentence catalog card body
    version: int              # monotonically increasing; bump on ANY template/schema change
    config: list[ConfigField]
    handlers: list[HandlerTemplate]     # ≥1; multi-handler bundles share the one config
    example_config: dict      # a complete, valid config used by registry/CI
                              # validation to render + lint every script (§4)
```

Model-level validators on `ExtensionManifest` (all raise `ValueError`, which
the registry converts to a startup failure):

- `slug`/field-name/key regexes as above; no duplicate `config` names; no
  duplicate handler `key`s; no duplicate handler `name`s.
- every `trigger_type` ∈ `ADMIN_HANDLER_TRIGGER_TYPES` (import from
  `smarter_dev.web.models` — single source of truth).
- `settings["include_bot_messages"]` only when `trigger_type == "message"`
  (mirror of `_reject_bot_optin_on_non_message`).
- `channel_scope` entries must name declared `channel_id`-typed fields.
- `schedule`/`timer` handlers: `settings` template must contain exactly one of
  the `handler_schedule` keys (`interval_seconds`/`daily_time` resp.
  `delay_seconds`/`fire_at`); values may be placeholders of a matching-typed
  field (`int` for the numeric keys, `string` for `daily_time`/`fire_at`). A
  schedule may additionally contain `start_at`; it must be a literal string or
  a placeholder for a `string` config field and render to an ISO-8601 timestamp
  with an explicit UTC offset.
- handler `name` ≤ 64 after strip (mirror of `_normalized_name`).

Schedule example (a nightly digest handler):

```python
HandlerTemplate(
    key="daily-digest",
    name="dm-relay-daily-digest",
    trigger_type="schedule",
    description="Posts a daily summary of relayed DMs to the staff log channel",
    script_file="daily_digest.monty",
    settings={"daily_time": "{{cfg.digest_time_utc}}"},
)
```

### 1.3 The dm-forum-relay extension as the reference manifest

`smarter_dev/extensions/catalog/dm_forum_relay/__init__.py`:

```python
MANIFEST = ExtensionManifest(
    slug="dm-forum-relay",
    title="DM ↔ Forum Relay",
    summary="Mirrors inbound DMs into a staff forum channel and relays staff "
            "replies in that forum back to the member over DM.",
    version=1,
    config=[
        ConfigField(name="forum_channel_id", type="channel_id",
                    label="Staff relay forum channel"),
        ConfigField(name="staff_role_id", type="role_id",
                    label="Staff role allowed to relay replies"),
        ConfigField(name="attribution_footer", type="string", required=False,
                    default="— relayed by staff",
                    label="Reply attribution footer"),
        ConfigField(name="notify_on_first_dm", type="bool", default=True,
                    label="Send the one-time monitoring notice"),
    ],
    handlers=[
        HandlerTemplate(
            key="dm-mirror",
            name="dm-relay-mirror",
            trigger_type="dm_message",
            description="Mirrors an inbound member DM into the relay forum",
            script_file="dm_mirror.monty",
            settings={},
            channel_scope=[],                       # dm_message is guild-scoped
        ),
        HandlerTemplate(
            key="forum-relay",
            name="dm-relay-forum-reply",
            trigger_type="message",
            description="Relays a staff reply in the relay forum back over DM",
            script_file="forum_relay.monty",
            settings={},
            channel_scope=["forum_channel_id"],     # row scoped to the forum channel
        ),
    ],
    example_config={
        "forum_channel_id": "123456789012345678",
        "staff_role_id": "234567890123456789",
        "attribution_footer": "— relayed by staff",
        "notify_on_first_dm": True,
    },
)
```

Both rows share the one install config; the mirror/relay handoff uses guild
shared memory (`guild_memory_*`, `GuildHandlerMemory`) exactly as designed in
`docs/v2/feature-parity/staff-communication-channels.md`.

---

## 2. Script templating and substitution

### 2.1 Placeholder syntax

`{{cfg.<field_name>}}` (regex
`\{\{\s*cfg\.([a-z][a-z0-9_]*)\s*\}\}`), usable in script templates and in
`settings` string values. `cfg.` is mandatory — a bare `{{name}}` never
matches, so Monty set/dict literals can never collide with the syntax, and the
post-render sweep (§2.4) only has to look for the `{{cfg.` prefix.

In a **script**, a placeholder must stand where a Python *expression* is
expected, bare (not inside quotes) — the renderer emits a complete typed
literal:

```python
# dm_mirror.monty (template)
FORUM_CHANNEL_ID = {{cfg.forum_channel_id}}
STAFF_ROLE_ID = {{cfg.staff_role_id}}
FOOTER = {{cfg.attribution_footer}}
SEND_NOTICE = {{cfg.notify_on_first_dm}}
```

renders (given the example config) to:

```python
FORUM_CHANNEL_ID = "123456789012345678"
STAFF_ROLE_ID = "234567890123456789"
FOOTER = "— relayed by staff"
SEND_NOTICE = True
```

House template style (enforced by review, not code): bind every placeholder
once to an UPPER_CASE constant at the top of the script and use the constant
below. This keeps rendered scripts readable in the existing handlers-admin
script viewer and makes the literal rails trivially auditable.

### 2.2 Typed substitution rules (`smarter_dev/extensions/rendering.py`)

All functions are pure (no I/O, no session):

```python
class RenderError(Exception): ...

def validate_config_values(manifest: ExtensionManifest, config: dict) -> dict:
    """Coerce+validate a raw config against the schema; return the cleaned dict.

    - unknown keys -> RenderError; missing required field -> RenderError;
      missing optional field -> default (or omitted if default is None).
    - channel_id / role_id: str matching ^[0-9]{15,20}$ (a Discord snowflake).
      This is the injection guard: an id value can never contain a quote,
      newline, or code fragment.
    - string: str, ≤ 500 chars, no NUL.
    - int: int (bool rejected); bool: bool.
    """

def render_script(template: str, manifest, config: dict) -> str:
    """Substitute every {{cfg.*}} with a typed literal:

    - channel_id / role_id -> '"' + value + '"'   (safe: snowflake-validated)
    - string               -> json.dumps(value)   (a valid Monty/Python string
                                                   literal, all escaping handled)
    - int                  -> str(value)
    - bool                 -> "True" / "False"
    """

def render_settings(settings_template: dict, manifest, config: dict) -> dict:
    """Recursively walk dicts/lists. A string value that is EXACTLY one
    placeholder is replaced by the *typed* value (str for ids/strings, int,
    bool) — so {"interval_seconds": "{{cfg.every}}"} yields a real int and
    {"allowed_role_ids": ["{{cfg.staff_role_id}}"]} yields a list[str].
    A string with embedded placeholders gets str(value) spliced in."""

def rendered_handler(handler: HandlerTemplate, manifest, config: dict,
                     script_template: str) -> RenderedHandler:
    """Bundle one handler's rendered artifacts (frozen dataclass):
    key, name, trigger_type, description, script, settings, channel_ids
    (channel_ids = [config[f] for f in handler.channel_scope]). The raw
    script_template text is passed in (loaded by the registry), not read here —
    rendering stays pure with no filesystem access."""

def render_bundle(manifest: ExtensionManifest, config: dict,
                  scripts: dict[str, str]) -> list[RenderedHandler]:
    """validate_config_values, render every handler, run every check in §2.4.
    ``scripts`` maps handler key -> raw template text (the registry loads it).
    Raises RenderError before ANY DB work can start — the installer calls this
    first, so a failed render can never leave partial rows."""

def extract_granted_role_literals(script: str) -> set[str]:
    """The string-literal second args of add_role/remove_role calls (same
    regex family as handler_lint._ROLE_ID_LITERAL, but capturing the id)."""
```

### 2.3 Deriving `allowed_role_ids` and `channel_ids` from config

- **`allowed_role_ids`** is not synthesized implicitly — the manifest states it
  in the handler's `settings` template
  (`{"allowed_role_ids": ["{{cfg.staff_role_id}}"]}`), exactly the shape the
  fire path reads (`admin_handlers_jobs.py:159`). §2.4's cross-check makes it
  impossible to ship a template whose script grants a role missing from the
  rendered allowlist. Explicit-in-manifest keeps the reviewed artifact the
  complete truth (nothing is injected behind the reviewer's back).
- **`channel_ids`** comes from `channel_scope`: the listed `channel_id` fields'
  values, in order. `[]` = guild-wide (the `AdminHandler.channel_ids` contract).

### 2.4 Render-time validation (every install / config-edit / update)

`render_bundle` fails (→ `RenderError` with a one-line reason) unless ALL of:

1. Every placeholder in every script, `settings` template, resolves to a
   declared field (`cfg.unknown` → error).
2. After substitution, no `{{cfg.` sequence remains anywhere (catches typos
   like `{{cfg.foo}` that the placeholder regex skipped).
3. `handler_lint.lint_script(rendered_script)` returns `None` for every
   handler — the same static rails + Monty parse the authoring pipeline runs
   (banned tokens, opaque blobs, `delete_thread` literal ban, role-id
   literality, defined-but-never-called). The judge stage is skipped by
   design; lint is not.
4. Role-grant allowlist closure: for each handler,
   `extract_granted_role_literals(script) ⊆ rendered settings["allowed_role_ids"]`
   (and the settings key must exist if the set is non-empty).
5. `schedule`/`timer` handlers:
   `handler_schedule.validate_time_trigger_settings(trigger_type,
   rendered_settings, uses_agent="spawn_agent(" in script)` passes and
   `first_fire_at(trigger_type, rendered_settings, now)` does not raise. This
   validates the rendered `start_at` UTC timestamp and recurrence floors against
   the *actual* config values. (The agent-spawn token is `spawn_agent(`, the
   name the handler runtime exposes.)
6. Registry-declared static checks already passed at import (§4); these five
   run again per-install because config values change the rendered output.

---

## 3. DB schema

### 3.1 New table: `extension_installs`

`smarter_dev/web/models.py`, placed after `GuildHandlerMemory` in the agentic
handler section:

```python
class ExtensionInstall(Base):
    """One guild's installation of a catalog extension.

    Records which extension (slug), at which catalog version, with which admin-
    supplied config, and owns the materialized ``admin_handlers`` rows via
    ``AdminHandler.extension_install_id`` — uninstall deletes exactly those.
    One install per (guild, extension).
    """

    __tablename__ = "extension_installs"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "extension_slug", name="uq_extension_installs_guild_slug"
        ),
        Index("ix_extension_installs_guild_id", "guild_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    guild_id: Mapped[str] = mapped_column(String(20), nullable=False)
    extension_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    # The catalog version materialized by the last install/update — compared
    # against the in-repo manifest's version for the "update available" badge.
    installed_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The cleaned config (validate_config_values output) the current rows were
    # rendered from; re-rendered on config-edit and update.
    config: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Skrift admin identity (username/email) who performed the install.
    installed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
```

### 3.2 `admin_handlers` additions

Two nullable columns on `AdminHandler` (NULL ⇒ hand-authored; the service only
ever mutates/deletes rows whose `extension_install_id` matches its install):

```python
    # Set when this row was materialized by an extension install; NULL for
    # hand-authored handlers. No FK by house style (cf. handler_runs.handler_id)
    # — the install service owns integrity.
    extension_install_id: Mapped[Optional[UUID]] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=True
    )
    # The manifest HandlerTemplate.key this row materializes — update and
    # config-edit match rows by (extension_install_id, key) so per-handler
    # memory survives re-renders.
    extension_handler_key: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
```

Plus `Index("ix_admin_handlers_extension_install_id", "extension_install_id")`
in `__table_args__`.

### 3.3 Alembic migration

As built: `alembic/main/versions/20260721_120000_7aa20a55c255_extension_installs.py`,
revision `7aa20a55c255`, `down_revision = "b7d9f1a3c5e2"` (the head at
implementation time — the design was drafted against an older head,
`c3d5e7f9a1b2`). Follows the `guild_handler_memory` pattern exactly:

```python
def upgrade() -> None:
    op.create_table(
        "extension_installs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("extension_slug", sa.String(length=64), nullable=False),
        sa.Column("installed_version", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("installed_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("guild_id", "extension_slug",
                            name="uq_extension_installs_guild_slug"),
    )
    op.create_index("ix_extension_installs_guild_id", "extension_installs",
                    ["guild_id"])
    op.add_column("admin_handlers",
                  sa.Column("extension_install_id", UUID(as_uuid=True), nullable=True))
    op.add_column("admin_handlers",
                  sa.Column("extension_handler_key", sa.String(length=64), nullable=True))
    op.create_index("ix_admin_handlers_extension_install_id", "admin_handlers",
                    ["extension_install_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_handlers_extension_install_id", table_name="admin_handlers")
    op.drop_column("admin_handlers", "extension_handler_key")
    op.drop_column("admin_handlers", "extension_install_id")
    op.drop_index("ix_extension_installs_guild_id", table_name="extension_installs")
    op.drop_table("extension_installs")
```

Register `"extension_installs"` in `MAIN_TABLES` in `alembic/main/env.py`
(commit-f6969da precedent; `tests/test_migration_ownership.py` fails the build
if forgotten).

---

## 4. Registry loading + validation

`smarter_dev/extensions/registry.py`:

```python
class ExtensionRegistryError(Exception):
    """A malformed catalog manifest — raised at load, never swallowed."""


@dataclass(frozen=True)
class LoadedExtension:
    manifest: ExtensionManifest
    scripts: dict[str, str]          # handler key -> raw template text


class ExtensionRegistry:
    def all(self) -> list[LoadedExtension]: ...      # slug-sorted, for the catalog page
    def get(self, slug: str) -> LoadedExtension: ... # KeyError -> ExtensionRegistryError


def load_registry() -> ExtensionRegistry: ...
def get_registry() -> ExtensionRegistry: ...          # module-level cached singleton
```

`load_registry()`:

1. Iterates `pkgutil.iter_modules` over `smarter_dev.extensions.catalog`,
   imports each subpackage, and requires a `MANIFEST` attribute of type
   `ExtensionManifest` — anything else → `ExtensionRegistryError` naming the
   module. No dynamic paths, no entry points: the catalog is exactly what's in
   the repo.
2. Reads each `script_file` (missing file → error); duplicate slugs across
   packages → error.
3. Runs `render_bundle(manifest, manifest.example_config, loaded.scripts)` for
   every extension — so ALL of §2.4 (placeholder resolution, lint, role-allowlist
   closure, schedule validity) is proven at load time against the shipped
   example config. A catalog change that breaks a template cannot start the
   app.

Fail-fast wiring: the admin controller module (§6) calls `get_registry()` at
import time (module level), and `app.yaml` registers that controller — so app
startup imports it and a malformed manifest kills boot with the
`ExtensionRegistryError` message. `tests/test_extension_registry.py` (§7) runs
the same load in CI so the failure is seen long before deploy. Note the
registry deliberately does NOT import `pydantic_monty` at module scope — lint's
`compiles()` already lazy-imports it, matching the web tier's import-hygiene
comments in `admin_handlers_jobs.py`.

---

## 5. Install / config-edit / update / enable / disable / uninstall semantics

Service module: `smarter_dev/web/extension_installs.py`. All public functions
take the caller's `AsyncSession`, do their own `commit()`, and raise
`ExtensionInstallError(message)` (module-local, message is flash-ready) on any
domain failure. The controller never touches `AdminHandler` directly.

```python
class ExtensionInstallError(Exception): ...

async def list_installs(session, guild_id: str) -> list[ExtensionInstall]
async def get_install(session, guild_id: str, slug: str) -> ExtensionInstall | None

async def install_extension(
    session, *, guild_id: str, slug: str, raw_config: dict, installed_by: str
) -> ExtensionInstall

async def edit_extension_config(
    session, *, guild_id: str, slug: str, raw_config: dict
) -> ExtensionInstall

async def update_extension(session, *, guild_id: str, slug: str) -> ExtensionInstall

async def set_extension_enabled(
    session, *, guild_id: str, slug: str, enabled: bool
) -> ExtensionInstall

async def uninstall_extension(session, *, guild_id: str, slug: str) -> None
```

Shared internals:

```python
async def _locked_install(session, guild_id, slug) -> ExtensionInstall
    # SELECT ... WHERE guild_id=... AND extension_slug=... FOR UPDATE;
    # None -> ExtensionInstallError("not installed")

async def _sync_handler_rows(
    session, install: ExtensionInstall, loaded: LoadedExtension, config: dict
) -> None
    # The one reconciliation primitive used by install, config-edit, and update.

async def _cancel_scheduled_job(record: AdminHandler) -> None
    # best-effort get_handle(job_id).cancel(), mirroring admin_handlers._reschedule

async def _arm_time_trigger(record: AdminHandler) -> None
    # first_fire_at + worker_submit(AdminHandlerFirePayload...) + record.scheduled_job_id,
    # extracted logic identical to api_native/admin_handlers._reschedule
```

### 5.1 Install

1. `loaded = get_registry().get(slug)`.
2. `render_bundle(loaded.manifest, raw_config, loaded.scripts)` — **all
   rendering and validation completes before any DB write** (`RenderError` →
   `ExtensionInstallError`).
3. Rails, inside one transaction:
   - guild handler cap: `count(admin_handlers where guild_id) +
     len(handlers) <= MAX_ADMIN_HANDLERS_PER_GUILD` else error.
   - name collisions: any rendered `name` already taken in the guild
     (`uq_admin_handlers_guild_name`) → error naming the conflicting handler
     ("rename or delete the existing admin handler '<name>' first").
4. Insert the `ExtensionInstall` row (config = cleaned config,
   `installed_version = manifest.version`), `flush()` — a concurrent duplicate
   install loses on `uq_extension_installs_guild_slug`; catch
   `IntegrityError`, rollback, raise "already installed in this guild".
5. Insert one `AdminHandler` per rendered handler with
   `extension_install_id=install.id`, `extension_handler_key=key`,
   `created_by_admin="extension"` (fits `String(20)`; precedent:
   `bot_admin/repeating_messages._CREATED_BY = "admin"`), `enabled=True`.
6. For `schedule`/`timer` rows: `_arm_time_trigger` (before commit — identical
   ordering to `create_admin_handler`; a crash between arm and commit leaves
   only a queue job that fires to `{"status": "missing"}`).
7. `commit()`.

**Atomicity:** steps 4–6 share one transaction — any failure (name race under
the unique index, schedule arming `ScheduleError`, connection loss) rolls back
the install row *and* every handler row together. There is no partial-install
state; the only possible orphan is a pre-armed queue job, which is already the
codebase's accepted no-op failure envelope.

### 5.2 `_sync_handler_rows` (config-edit + update)

Given the locked install, the target `LoadedExtension`, and cleaned config:

1. `rendered = render_bundle(manifest, config, loaded.scripts)` (again: fully
   validated before any write).
2. Load owned rows: `select(AdminHandler).where(extension_install_id ==
   install.id)`; index by `extension_handler_key`.
3. Reconcile by key:
   - **existing key** → update `name`, `description`, `script`, `settings`,
     `channel_ids` in place (row `id` and `memory` preserved — a re-render
     never wipes handler state); if the row is a time trigger,
     `_cancel_scheduled_job` + (if `install.enabled`) `_arm_time_trigger`.
     Deviation from the bot-API edit path recorded in §9: `update_admin_handler`
     force-sets `enabled=True` on edit; sync instead sets each row's `enabled`
     to `install.enabled`, because extension rows' enablement is owned by the
     install toggle.
   - **new key** (update introduced a handler) → insert as in §5.1.5–6,
     `enabled=install.enabled`, arm only if enabled.
   - **stale key** (update removed a handler) → `_cancel_scheduled_job`,
     `session.delete(row)`.
   - `trigger_type` changed for a key → treated as delete + insert (new row
     id, memory reset) — trigger type is immutable per row
     (`update_admin_handler` precedent), and the CHECK constraint plus
     dispatch semantics make in-place mutation a trap.
4. Cap/name-collision rails re-checked for the post-sync state.

**Config-edit** = `_locked_install` → `validate_config_values` (against the
*installed* version's manifest? No — see §9: the registry only holds the
current version, so config-edit implies update; the UI presents them as one
"Save & apply" that also advances `installed_version`) → `_sync_handler_rows`
→ set `install.config`, `install.installed_version = manifest.version`,
`install.updated_at` → commit.

**Update** = the same call with `raw_config = install.config` re-validated
against the new manifest. If the new version added a required field with no
default, `validate_config_values` fails and the service raises — the UI routes
the admin to the config form pre-filled with the old values instead (§6.3).

### 5.3 Enable / disable

`set_extension_enabled` (under the row lock):

- **disable**: `install.enabled = False`; for every owned row:
  `enabled = False`, `_cancel_scheduled_job` (clears `scheduled_job_id`). The
  fire job already refuses disabled rows (`admin_handlers_jobs` returns
  `"missing"`) and the recurring chain self-stops, so cancel is belt-and-braces
  the same way `delete_admin_handler`'s cancel is.
- **enable**: `install.enabled = True`; every owned row `enabled = True`; re-arm
  each `schedule`/`timer` row via `_arm_time_trigger`.
- Commit. The bot's `active-channels` poll picks the change up within its
  refresh interval; no push required.

### 5.4 Uninstall

Under the row lock: for every owned row `_cancel_scheduled_job` +
`session.delete(row)`; `session.delete(install)`; commit. Only rows with
`extension_install_id == install.id` are ever touched — hand-authored handlers
(NULL) and other installs' rows are structurally out of reach. `handler_runs`
audit rows are retained (they reference `handler_id` without FK, matching the
existing delete path, which also leaves runs behind).

### 5.5 Concurrency summary

- Same-install mutators serialize on `SELECT ... FOR UPDATE` of the
  `extension_installs` row.
- Double-install races collapse on `uq_extension_installs_guild_slug`.
- Extension rows vs. the Discord-side admin authoring pipeline: an admin
  *could* edit/delete an extension-owned row via the bot API (it has no
  knowledge of `extension_install_id`). Out of scope to block in slice A/B;
  §9 records the follow-up (teach `update/delete_admin_handler` to 409 on
  rows with a non-NULL `extension_install_id`).

---

## 6. Admin UI

### 6.1 Controller

`smarter_dev/web/bot_admin/extensions.py`, class
`ExtensionsAdminController(Controller)`, `path = "/admin/bot"`, registered in
`app.yaml` under `controllers:` as
`smarter_dev.web.bot_admin.extensions:ExtensionsAdminController`. Module level:
`_ACTIVE_PAGE = "extensions"` and `_registry = get_registry()` (the §4
fail-fast import). Every route:
`guards=[auth_guard, Permission("administrator")]` (per-route, matching every
sibling controller).

Routes:

| Verb | Path (under `/admin/bot`) | Handler | Purpose |
|---|---|---|---|
| GET | `/guilds/{guild_id:str}/extensions` | `extensions_list` | Catalog + installed state |
| GET | `/guilds/{guild_id:str}/extensions/{slug:str}/install` | `extension_install_form` | Blank config form |
| POST | `/guilds/{guild_id:str}/extensions/{slug:str}/install` | `extension_install` | Validate + install |
| GET | `/guilds/{guild_id:str}/extensions/{slug:str}/configure` | `extension_configure_form` | Form prefilled from `install.config` |
| POST | `/guilds/{guild_id:str}/extensions/{slug:str}/configure` | `extension_configure` | Re-render rows with new config (also applies pending update, §5.2) |
| POST | `/guilds/{guild_id:str}/extensions/{slug:str}/update` | `extension_update` | One-click update at current config |
| POST | `/guilds/{guild_id:str}/extensions/{slug:str}/enable` | `extension_enable` | `set_extension_enabled(True)` |
| POST | `/guilds/{guild_id:str}/extensions/{slug:str}/disable` | `extension_disable` | `set_extension_enabled(False)` |
| POST | `/guilds/{guild_id:str}/extensions/{slug:str}/uninstall` | `extension_uninstall` | Remove install + owned rows |

Shared handler shape (the `repeating_messages` pattern verbatim): `guild,
error = await fetch_guild_or_error(request, db_session, guild_id)`; unknown
slug → `flash_error` + redirect to the list; POSTs parse
`await request.form()`, call the §5 service, `flash_success`/`flash_error(str(exc))`
on `ExtensionInstallError`, and `Redirect(path=f"/admin/bot/guilds/{guild_id}/extensions")`.
`installed_by` = the Skrift admin identity off the request session (same
lookup `get_admin_context` uses; fall back to `"admin"`).

Form parsing/validation are pure module functions (mirrors
`read_repeating_message_form`/`validate_repeating_message_form`):

```python
def read_extension_config_form(manifest: ExtensionManifest, form) -> dict
    # str values for channel_id/role_id/string/int fields; bool fields:
    # present-and-"true" -> True else False (checkbox convention).

def validate_extension_config(manifest, raw: dict) -> tuple[bool, list[str], dict]
    # wraps rendering.validate_config_values; returns (ok, human errors, cleaned)
    # so the form re-renders field-level messages without exceptions.
```

Validation failure re-renders the form template with `errors`, the entered
values, and `status_code=400` (the sibling controllers' convention).

### 6.2 Templates

Under `templates/admin/bot/extensions/`:

- **`list.html`** — sidebar include (`{% include "admin/bot/_sidebar.html" %}`
  pattern of the sibling pages) + one card per `registry.all()` entry:
  title, summary, `v{{ manifest.version }}`, the extension's handler count and
  trigger types. Per-card state from `installs_by_slug`:
  - not installed → **Install** link (`.../{slug}/install`).
  - installed → Enabled/Disabled badge, **Configure** link,
    **Enable**/**Disable** and **Uninstall** as inline POST forms
    (uninstall has an `onsubmit="return confirm(...)"` guard, the pattern used
    by the delete buttons in `admin/handlers/list.html`).
  - `manifest.version > install.installed_version` → an
    "Update available (v{{ install.installed_version }} → v{{ manifest.version }})"
    badge + **Update** POST button.
- **`config_form.html`** — shared by install and configure (context flag
  `mode: "install" | "configure"`). Iterates `manifest.config` and renders per
  `field.type`:
  - `channel_id` → `<select name="{{ field.name }}">` over `channels`
    (id + name), with a fallback `<input type="text" pattern="[0-9]{15,20}">`
    when `channels` is empty (Discord fetch degraded).
  - `role_id` → same over `roles`.
  - `string` → `<input type="text">`; `int` → `<input type="number">`;
    `bool` → `<input type="checkbox" value="true">`.
  - value precedence: submitted value (on validation error) → `install.config`
    (configure) → `field.default` → empty. `field.help` under the input;
    required fields marked.

Channel/role dropdown data: `channels = await
get_admin_discord_client().get_guild_channels(guild_id)` and
`get_guild_roles(guild_id)`, each wrapped in the degrade-to-empty pattern of
`load_channels_and_roles` (`bot_admin/repeating_messages.py:359`) — a Discord
outage must not block the page. (Deviation from that helper: it fetches only
announcement channels; extensions need the full channel list, so the
controller uses `get_guild_channels` directly.)

Sidebar: add to `templates/admin/bot/_sidebar.html` inside the `{% if
guild_id %}` block, after "Audit Logs":

```html
<li><a href="/admin/bot/guilds/{{ guild_id }}/extensions" class="sk-btn-outline sk-btn-small"
       style="display:block; text-align:left;{% if active_page == 'extensions' %} font-weight:bold;{% endif %}">Extensions</a></li>
```

### 6.3 Update-with-missing-config flow

`extension_update` calls `update_extension`; on the specific failure "config
invalid against the new schema" (`ExtensionInstallError` subclass
`ExtensionConfigOutdatedError`), it redirects to `.../configure` with a flash
telling the admin which fields are missing — the configure POST then performs
the combined save-and-update (§5.2).

---

## 7. Test plan

All async DB tests use the existing suite's session fixtures
(`tests/web/conftest.py` conventions); pure functions get plain unit tests.
Run with `uv run pytest` (see MEMORY note on bare-run quirks).

**`tests/test_extension_registry.py`** (pure + import-level):
- `load_registry()` succeeds on the shipped catalog; every manifest's
  `example_config` renders and lints clean (this is the CI fail-fast).
- Malformed fixtures (built in-test, not on disk): missing `MANIFEST`, dup
  slug, dup handler key, unknown trigger_type, `include_bot_messages` on a
  `dm_message` handler, missing script file, `channel_scope` naming a
  non-channel field, schedule handler without a timing key → each raises
  `ExtensionRegistryError`/`ValueError` with the offending name in the message.

**`tests/test_extension_rendering.py`** (pure):
- Typed substitution per field type (ids quoted, strings json-escaped incl.
  quotes/newlines, int bare, bool `True`/`False`).
- `validate_config_values`: snowflake regex rejects `123"; evil()`, non-digit,
  wrong length; required/missing/default/unknown-key behavior; bool rejected
  for int.
- Unresolved placeholder, leftover `{{cfg.` after render, lint failure of a
  rendered script, role-grant literal missing from rendered
  `allowed_role_ids`, sub-floor `interval_seconds` → `RenderError` each.
- `render_settings` recursion: exact-placeholder string → typed value inside
  list and nested dict; embedded placeholder → spliced str.

**`tests/web/test_extension_install_service.py`** (DB):
- Install happy path: install row + N `AdminHandler` rows with correct
  `extension_install_id`/`extension_handler_key`/`created_by_admin="extension"`;
  schedule handler got a `scheduled_job_id`; rendered script/settings match.
- Atomicity: force a name collision on the bundle's *second* handler
  (pre-create a hand-authored row with that name) → install raises and **zero**
  rows exist (no install row, no first handler).
- Double install → `ExtensionInstallError`; guild cap (pre-create 19 handlers,
  install a 2-handler bundle) → error, nothing written.
- Config-edit: rows re-rendered in place — same row ids, `memory` preserved,
  new literals present; time trigger rescheduled (old job cancelled).
- Update: version with an added handler key creates it; removed key deletes
  it (job cancelled); changed `trigger_type` swaps the row; hand-authored rows
  in the guild untouched throughout; `installed_version` advanced; update with
  a new required field and no default → `ExtensionConfigOutdatedError`, no
  changes.
- Enable/disable: all owned rows flip, `scheduled_job_id`
  cancelled-and-cleared on disable, re-armed on enable; install row flag
  matches; disabled install + config-edit keeps rows disabled.
- Uninstall: owned rows + install gone; hand-authored and other-install rows
  intact; `handler_runs` for the deleted rows retained.
- Concurrency: two concurrent `install_extension` for the same (guild, slug)
  → exactly one install row and one bundle of handlers.

**`tests/web/test_extensions_admin.py`** (controller — drives handlers via
`.fn(...)` with monkeypatched services, the established sibling-suite pattern,
rather than the `TestClient`/HTTP approach this design originally sketched):
- Every route declares `guards=[auth_guard, Permission("administrator")]`, and
  `Permission("administrator").check` denies a non-admin (auth asserted
  structurally, matching sibling controller tests).
- List page: catalog renders; installed extension shows configure/disable/
  uninstall; version bump shows the update badge + button.
- Install GET renders a field per `ConfigField` with the right input type;
  channel/role selects present when the Discord client is stubbed, text
  fallback when it errors.
- Install POST: happy path redirects with flash + rows exist; invalid
  snowflake re-renders 400 with the error and the entered values.
- Configure POST, update POST, enable/disable POST, uninstall POST each drive
  the service and redirect (service verified above; here assert wiring +
  flash).

**Migration layer:**
- `tests/test_migration_ownership.py` passes unmodified (proves the
  `MAIN_TABLES` registration).
- Existing migration up/down smoke conventions apply to the new revision.

---

## 8. Work split: two implementation slices

### Slice A — registry + install service + migration (no UI)

Files:
- `smarter_dev/extensions/{__init__,schema,rendering,registry}.py`
- `smarter_dev/extensions/catalog/dm_forum_relay/` (manifest + `dm_mirror.monty`,
  `forum_relay.monty` — scripts per
  `docs/v2/feature-parity/staff-communication-channels.md`)
- `smarter_dev/web/models.py`: `ExtensionInstall` + the two `AdminHandler`
  columns/index
- `alembic/main/versions/20260721_120000_*_extension_installs.py` +
  `MAIN_TABLES` entry
- `smarter_dev/web/extension_installs.py` (service)
- Tests: `tests/test_extension_registry.py`, `tests/test_extension_rendering.py`,
  `tests/web/test_extension_install_service.py`

### Slice B — admin UI

Files:
- `smarter_dev/web/bot_admin/extensions.py` (`ExtensionsAdminController` +
  `read_extension_config_form`/`validate_extension_config`)
- `templates/admin/bot/extensions/{list.html,config_form.html}`
- `templates/admin/bot/_sidebar.html` (one `<li>`)
- `app.yaml` controllers entry
- Tests: `tests/web/test_extensions_admin.py`

### The pinned interface between A and B

Slice B may use **only** these names; slice A must not change them without a
matching B change:

```python
# smarter_dev.extensions.registry
get_registry() -> ExtensionRegistry
ExtensionRegistry.all() -> list[LoadedExtension]
ExtensionRegistry.get(slug: str) -> LoadedExtension   # ExtensionRegistryError if unknown
LoadedExtension.manifest: ExtensionManifest           # .slug .title .summary .version
                                                      # .config: list[ConfigField]
ConfigField: .name .type .label .help .required .default

# smarter_dev.extensions.rendering
validate_config_values(manifest, config) -> dict      # raises RenderError (str(exc) is user-facing)

# smarter_dev.web.extension_installs
ExtensionInstallError                                  # str(exc) is flash-ready
ExtensionConfigOutdatedError(ExtensionInstallError)
list_installs(session, guild_id) -> list[ExtensionInstall]
get_install(session, guild_id, slug) -> ExtensionInstall | None
install_extension(session, *, guild_id, slug, raw_config, installed_by) -> ExtensionInstall
edit_extension_config(session, *, guild_id, slug, raw_config) -> ExtensionInstall
update_extension(session, *, guild_id, slug) -> ExtensionInstall
set_extension_enabled(session, *, guild_id, slug, enabled) -> ExtensionInstall
uninstall_extension(session, *, guild_id, slug) -> None

# smarter_dev.web.models.ExtensionInstall (read-only to B)
.guild_id .extension_slug .installed_version .config .enabled .updated_at
```

`raw_config` is the untyped form dict (all-string except bools); the service
owns cleaning via `validate_config_values`. B can therefore be built against a
stub service and land independently once A merges.

---

## 9. Recorded deviations & follow-ups

1. **Schedules are not cron.** The brief said "schedule-trigger handlers
   (cron-like)"; the codebase vocabulary is
   `interval_seconds`/`daily_time`/`delay_seconds`/`fire_at`
   (`handler_schedule.py`). Manifests use that vocabulary verbatim.
2. **Version is an integer**, not semver — a monotonic `version: int` makes
   "update available" a plain `>` against `installed_version` (Integer
   column). Semver adds parsing for zero benefit in a single-repo catalog.
3. **Config-edit implies update.** The registry holds only the current
   manifest version, so re-rendering on config-edit necessarily uses the
   newest templates and advances `installed_version`. A true "edit config at
   the old version" would require shipping historical templates — deliberately
   out of scope.
4. **Row `enabled` on sync.** The bot-API edit path force-sets `enabled=True`
   (`update_admin_handler`); extension sync instead propagates
   `install.enabled`, since enablement of extension rows is owned by the
   install toggle.
5. **`created_by_admin = "extension"`.** The column is `String(20)` (a Discord
   snowflake for bot-created rows); panel precedent is a literal
   (`repeating_messages._CREATED_BY = "admin"`). The real Skrift identity is
   on `ExtensionInstall.installed_by`.
6. **No FK constraints** for `extension_install_id` — house style in this model
   area (cf. `handler_runs.handler_id`, `guild_handler_memory`); integrity is
   service-owned and test-guarded.
7. **Judge skipped, lint kept.** First-party review replaces the judge stage,
   but `handler_lint.lint_script` runs on every rendered script at registry
   load (example config) and at every install/edit/update (real config).
8. **Follow-up (not in A/B):** the Discord-side admin authoring pipeline can
   still edit/delete extension-owned rows via
   `PUT/DELETE /api/admin/handlers/...`. Teach those routes to 409 on rows
   with a non-NULL `extension_install_id` ("managed by the '<slug>' extension —
   configure it in the admin panel").
9. **Follow-up:** surface extension-owned rows distinctly (badge + link to the
   install) in the existing `/admin` handlers review page
   (`handlers_admin.py`).
