# Stage 03 — Admin slash command + model-selection modal

**Stable id:** `slash-command-modal`

## Outcome
Add the admin-only slash command that opens a modal to pick a model for the
**current channel** and set daily + hourly token budgets (0 = unlimited), then
persists it via the stage-02 service. After this stage an admin can fully
configure a channel override through Discord and see it stored; the chat runtime
does not yet act on it (stage 04).

## Prior state (already committed)
- Stage 01: `model_catalog.py` — `MODEL_CATALOG`, `models_by_family()`,
  `get_model(key)`, `is_valid_model_key(key)`, `CatalogModel` with `key`/`label`/
  `family`/`provider`/`model_id`.
- Stage 02: `bot.d["model_override_service"]` (`ModelOverrideService`) with
  `get_override` / `set_override` / `clear_override`; web API persists it.

## Context you need (read before starting)
- **Admin gating**: copy the pattern in `smarter_dev/bot/plugins/admin_handlers.py`
  — `is_admin(permissions)` + `_deny_if_not_admin(ctx)` (checks
  `isinstance(ctx.member, hikari.InteractionMember)` and
  `lightbulb.utils.permissions_for(ctx.member) & hikari.Permissions.ADMINISTRATOR`,
  responds ephemerally and returns True when denied). There is **no** gating
  decorator in this codebase; gate inline at the top of the handler.
- **Slash command shape**: `smarter_dev/bot/plugins/warn.py` is the simplest
  standalone `SlashCommand` template. Plugins expose `load(bot)`/`unload(bot)` that
  call `bot.add_plugin`/`bot.remove_plugin`.
- **Modals**: raw hikari builders in `smarter_dev/bot/views/`. Best template:
  `smarter_dev/bot/views/beacon_views.py` — `create_beacon_message_modal()` builds
  `hikari.impl.InteractionModalBuilder(title, custom_id)` +
  `ModalActionRowBuilder().add_component(TextInputBuilder(...))`; shown from a slash
  command via `ctx.respond_with_modal(modal.title, modal.custom_id, components=modal.components)`
  (see `squads.py` `/beacon`). Read submitted values by iterating
  `event.interaction.components` → action_row → matching `custom_id` → `.value`
  (see `beacon_views.py`).
- **Modal submit routing is manual**: `smarter_dev/bot/plugins/events.py`
  `handle_modal_interaction(event)` dispatches by `custom_id` (prefix/exact). The
  single global listener is registered in `setup_interaction_handlers(bot)`
  (`events.py`, calls `handle_component_interaction` then `handle_modal_interaction`).
  A new modal `custom_id` **must** be added to that dispatch or it logs "Unhandled
  modal interaction" and silently does nothing. If you use the two-step fallback
  (below), the string-select interaction is routed via `handle_component_interaction`
  in the same file — register your select `custom_id` there too.
- **Plugin loading**: add `bot.load_extensions("smarter_dev.bot.plugins.model_override")`
  in `load_plugins()` in `smarter_dev/bot/client.py` (~lines 1088–1176).
- **Service access from handlers**: `ctx.bot.d["model_override_service"]` (or
  `bot.d["_services"]["model_override_service"]`), matching how commands reach
  `bytes_service`.
- Global rules: fail fast; specific exceptions only; no inline imports; `X | None`;
  builtins for annotations; TDD.

## Discord modal component decision (IMPORTANT — resolve first)
A Discord modal historically allowed only **text inputs**. Selecting a model is
best as a dropdown, but dropdown-in-modal support depends on the installed hikari
+ current Discord API. **Check support before choosing a path:**

```
uv run python -c "import hikari, hikari.impl as i; print(hikari.__version__); print([n for n in dir(i) if 'Select' in n or 'Label' in n or 'Modal' in n])"
```

- **Path A (preferred, single modal)** — if the installed hikari/Discord supports a
  string select (and label component) inside `InteractionModalBuilder`: build ONE
  modal with a string-select of models (options from `models_by_family()`, grouped
  visually by prefixing the label with the family, e.g. `"Qwen · Qwen3 32B"`; value
  = catalog `key`), plus two `TextInputBuilder`s (`daily_budget`, `hourly_budget`,
  short style, numeric, `required=False`, placeholder "0 = unlimited"). Add a
  first select option `"Server default (remove override)"` with a sentinel value
  like `__default__`.
- **Path B (fallback, two-step)** — if selects-in-modals are NOT supported: the
  slash command first responds (ephemeral) with a **message string-select**
  (`custom_id="model_override_select"`) listing the models + the sentinel. On
  select, open the budgets modal via
  `event.interaction.create_modal_response(title, custom_id, components=...)` where
  `custom_id` encodes the chosen key, e.g. `model_override_modal:<guild>:<channel>:<key>`.
  The modal then holds only the two budget text inputs. (This mirrors how
  `events.py` opens modals from component interactions elsewhere.)

Pick one path, implement it, and record the choice + hikari version in the commit
message. Keep ≤ 25 select options (the catalog is ≤ 24 + the sentinel = 25 max —
if a verified catalog exceeds 24, drop to one model per family for the select).

## What to build

### 1. Views module
`smarter_dev/bot/views/model_override_views.py`:
- `build_model_options(current_key: str | None) -> list[...]` — pure builder of
  select options from `models_by_family()`, marking the current selection as
  default, plus the `__default__` sentinel option.
- Path A: `create_model_override_modal(current: OverrideDTO | None) -> InteractionModalBuilder`
  (custom_id `model_override_modal:<guild_id>:<channel_id>`; prefills budgets/
  selection from `current`).
  Path B: `create_model_select_message(current) -> components` and
  `create_budgets_modal(guild_id, channel_id, model_key, current) -> InteractionModalBuilder`.
- `parse_budget(raw: str | None) -> int` — pure: empty/None → 0; must be a
  non-negative integer; raise a `ValueError` (caught by the handler → ephemeral
  error) on anything else. 0 = unlimited.

### 2. Command plugin
`smarter_dev/bot/plugins/model_override.py`:
- `plugin = lightbulb.Plugin("model_override")`.
- One admin-gated `SlashCommand`, e.g. `/setmodel` — description "Set the LLM model
  and token budgets for this channel (admin only)". Copy `is_admin` +
  `_deny_if_not_admin`. On invoke: `if await _deny_if_not_admin(ctx): return`;
  fetch the current override via the service (to prefill); then Path A:
  `ctx.respond_with_modal(...)`, or Path B: respond with the select message.
- `load`/`unload` add/remove the plugin.

### 3. Submit + (Path B) select handlers in `events.py`
- Add a branch in `handle_modal_interaction` matching your modal `custom_id`
  prefix. Parse guild/channel (from custom_id and/or `event.interaction`), read the
  selected model key + budget values, then:
  - sentinel `__default__` → `model_override_service.clear_override(...)`.
  - otherwise validate `is_valid_model_key(key)`; `parse_budget` both budgets;
    `model_override_service.set_override(guild, channel, key, daily, hourly)`.
  - Respond ephemerally with a confirmation summarizing model + budgets (render
    `0` as "unlimited"). On `ValueError`/invalid key, respond ephemerally with a
    clear error (do NOT persist).
- Path B only: add a branch in `handle_component_interaction` for
  `model_override_select` that opens the budgets modal.
- Re-verify admin permission again on submit (defense in depth) — the modal can be
  submitted by anyone who somehow has the custom_id; check
  `event.interaction.member` permissions the same way, deny ephemerally otherwise.

### 4. Load the plugin
Add the `load_extensions("smarter_dev.bot.plugins.model_override")` line in
`client.py` `load_plugins()`.

## Tests (TDD — write first)
Mirror `tests/bot/test_commands.py` (import handler, call with a mocked lightbulb
context) and `tests/bot/test_views.py`:
- **Admin gate**: non-admin member → `_deny_if_not_admin` responds ephemerally and
  the modal/select is NOT shown, service NOT called. Admin → modal/select shown.
- **Views**: `build_model_options` includes every catalog model + the `__default__`
  sentinel, marks `current_key` as default, ≤ 25 options. `parse_budget`:
  `""`/`None`→0, `"0"`→0, `"1500"`→1500, `"-1"`/`"abc"`/`"1.5"`→`ValueError`.
- **Submit handler**: with a mocked `model_override_service` and a fake modal
  interaction (build one like `create_mock_discord_event` / the beacon modal
  tests), assert:
  - valid submit → `set_override` called with parsed args; ephemeral confirmation.
  - `__default__` → `clear_override` called.
  - invalid budget → ephemeral error, `set_override` NOT called.
  - non-admin submitter → denied, service NOT called.
- Full suite: `uv run pytest`.

## Acceptance criteria
- `/setmodel` appears as an admin-only command; opening it shows the modal
  (or select→modal on Path B) prefilled with any existing override.
- Submitting persists via `ModelOverrideService` (verified by re-running
  `/setmodel` showing the saved values, or by the stage-02 API/DB in tests).
- Modal `custom_id` is registered in `events.py` dispatch (no "Unhandled modal"
  log).
- `uv run pytest` green. Run `semgrep` + `gitleaks` before committing.

## Notes / decisions
- Record Path A vs B + hikari version in the commit message.
- The chat runtime does not consume the override until stage 04 — that is expected;
  the codebase remains fully working (the command stores config with no runtime
  side effect yet).
