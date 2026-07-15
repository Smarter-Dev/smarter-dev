# Stage 01 — Model catalog + provider routing (incl. Digital Ocean)

**Stable id:** `model-catalog-provider-routing`

## Outcome
Introduce a single source of truth for the selectable LLM models (Kimi, GLM,
DeepSeek, Gemma, Qwen, Gemini, GPT) and the code that builds a Pydantic AI
`Model` for any of them — routing Gemini→Google, GPT→OpenAI, and the rest→Digital
Ocean's OpenAI-compatible inference endpoint. This stage adds **no runtime
behavior change** (the chat agent still uses its default model); it only adds the
catalog + a `build_model_for(model_id)` helper + Digital Ocean config, all
unit-tested. Later stages consume this.

## Why this is first
Both the web API (stage 02, validates a submitted `model_id`) and enforcement
(stage 04, builds the override model) depend on the catalog and the provider
router. Building it standalone gives a clean, tested checkpoint.

## Context you need (read these before starting)
- `smarter_dev/bot/agents/chat_agent.py` — current model building. `_build_model()`
  (lines ~49–60) selects provider **by model-id prefix**: `gpt-`/`openai/` →
  `OpenAIResponsesModel` + `OpenAIProvider(api_key=OPENAI_API_KEY)`; otherwise
  `GoogleModel` + `GoogleProvider(api_key=GEMINI_API_KEY or GOOGLE_API_KEY)`.
  `_model_settings()` returns per-provider `ModelSettings`. Keys come from
  `os.getenv`, NOT from the settings class.
- `smarter_dev/llm_config.py` — DSPy-side provider resolution (`_get_provider_from_model`,
  prefix-based). Not used by the chat agent, but confirms the prefix convention.
- `smarter_dev/shared/config.py` — `Settings(BaseSettings)` (pydantic-settings).
  Add new fields here for the DO endpoint. Note existing LLM API **keys are not**
  in this class; DO's key/base-url are configuration we *will* add here so the
  endpoint is overridable, while still reading the secret via `os.getenv` fallback
  to match the existing key convention. Follow the existing field + `.env` style.
- `.env.example` at repo root — document new env vars here.
- Global instruction: **never use inline imports** in new modules. (The existing
  `chat_agent.py` uses inline imports for the optional OpenAI path; keep your new
  catalog module clean with top-level imports. You may leave `chat_agent.py`'s
  existing inline imports as-is, or, if you touch that function, prefer top-level.)
- Global instruction: no `typing.Optional`/`Union`; use `X | None`. No `typing.List`
  etc.; use builtins.

## What to build

### 1. Model catalog module
Create `smarter_dev/bot/agents/model_catalog.py` (bot-agent scope, since the chat
agent and its tooling live under `smarter_dev/bot/agents/`). Define:

- An enum `ModelProvider` with members `GOOGLE`, `OPENAI`, `DIGITALOCEAN`.
- A frozen dataclass `CatalogModel` with fields: `key: str` (stable slug used in
  the DB + custom_ids, e.g. `"kimi-k2"`), `label: str` (human label shown in the
  Discord select, e.g. `"Kimi K2 (Moonshot)"`), `family: str` (one of the seven
  families), `provider: ModelProvider`, `model_id: str` (the exact id passed to
  the provider SDK / sent as the `model` field).
- `MODEL_CATALOG: tuple[CatalogModel, ...]` — the curated list below.
- Helpers (pure): `catalog_by_key() -> dict[str, CatalogModel]`,
  `get_model(key: str) -> CatalogModel | None`, `is_valid_model_key(key) -> bool`,
  and `models_by_family() -> dict[str, list[CatalogModel]]` (preserves catalog
  order) for building a grouped select later.

**Curated catalog (families → provider).** Keep the total ≤ 24 entries so the whole
set fits in one Discord string-select (25-option limit, leaving room for a
"server default" sentinel added in stage 03). Aim for the latest 1–2 models per
family.

| family   | provider      | suggested `key`      | `model_id` (VERIFY — see note) | label |
|----------|---------------|----------------------|--------------------------------|-------|
| Kimi     | DIGITALOCEAN  | `kimi-k2`            | `moonshotai/kimi-k2-instruct`  | Kimi K2 |
| GLM      | DIGITALOCEAN  | `glm-4-6`            | `zai/glm-4.6`                  | GLM-4.6 |
| DeepSeek | DIGITALOCEAN  | `deepseek-v3-1`      | `deepseek-ai/deepseek-v3.1`    | DeepSeek V3.1 |
| DeepSeek | DIGITALOCEAN  | `deepseek-r1`        | `deepseek-ai/deepseek-r1`      | DeepSeek R1 |
| Gemma    | DIGITALOCEAN  | `gemma-3-27b`        | `google/gemma-3-27b-it`        | Gemma 3 27B |
| Qwen     | DIGITALOCEAN  | `qwen3-32b`          | `qwen/qwen3-32b`               | Qwen3 32B |
| Gemini   | GOOGLE        | `gemini-3-1-flash-lite` | `gemini-3.1-flash-lite`     | Gemini 3.1 Flash Lite |
| Gemini   | GOOGLE        | `gemini-3-flash`     | `gemini-3-flash-preview`       | Gemini 3 Flash |
| GPT      | OPENAI        | `gpt-5-4`            | `gpt-5.4`                      | GPT-5.4 |
| GPT      | OPENAI        | `gpt-5-4-nano`       | `gpt-5.4-nano`                 | GPT-5.4 nano |

**VERIFY the exact `model_id` strings at implementation time** — these are the
correct *shape* but provider slugs change. Confirm:
- Digital Ocean serverless-inference model slugs from the DO Gradient AI /
  serverless-inference model catalog (the slug is what goes in the OpenAI-compatible
  `model` field). Use `WebSearch`/DO docs to get current slugs for Kimi, GLM,
  DeepSeek, Gemma, Qwen.
- Gemini ids: match what the codebase already uses (`gemini-3.1-flash-lite`,
  `gemini-3-flash-preview` appear across `web/`/`bot/agents/`). Reuse those exact
  strings so pricing patches in `web/llm_pricing.py` keep working.
- GPT ids: match codebase usage (`gpt-5.4`, `gpt-5.4-nano`).
It is fine to add/drop a model per family based on what each provider currently
serves — the catalog is the point of change. Keep `key` values stable once chosen
(they are persisted in the DB in stage 02).

### 2. Digital Ocean config
In `smarter_dev/shared/config.py` `Settings`, add:
- `digitalocean_inference_base_url: str = "https://inference.do-ai.run/v1"`
  (**VERIFY** the DO OpenAI-compatible base URL from DO docs; this is the Gradient
  serverless-inference endpoint).
- Do NOT add the DO secret as a required settings field — read it via `os.getenv`
  in the router below to match the existing key convention (`OPENAI_API_KEY` etc.
  are read from env, not Settings). The env var name is
  `DIGITALOCEAN_INFERENCE_API_KEY`.

Document both in `.env.example`:
```
# Digital Ocean serverless inference (OpenAI-compatible) — hosts Kimi/GLM/DeepSeek/Gemma/Qwen
DIGITALOCEAN_INFERENCE_API_KEY=
# Optional override; defaults to https://inference.do-ai.run/v1
DIGITALOCEAN_INFERENCE_BASE_URL=
```

### 3. Provider router
Create `smarter_dev/bot/agents/model_router.py` with a pure-ish builder:

```
def build_model_for(model: CatalogModel) -> Model: ...
def model_settings_for(model: CatalogModel) -> ModelSettings | None: ...
```

Routing:
- `GOOGLE` → `GoogleModel(model.model_id, provider=GoogleProvider(api_key=GEMINI_API_KEY or GOOGLE_API_KEY))`
  and `GoogleModelSettings(google_thinking_config={"thinking_level": "MEDIUM"})`
  (mirror `chat_agent.py`).
- `OPENAI` → `OpenAIResponsesModel(model.model_id, provider=OpenAIProvider(api_key=OPENAI_API_KEY))`
  and `OpenAIResponsesModelSettings(openai_reasoning_effort="low")` (mirror
  `chat_agent.py`).
- `DIGITALOCEAN` → use the **Chat Completions** OpenAI-compatible model
  (`from pydantic_ai.models.openai import OpenAIChatModel`), NOT the Responses
  API model (DO exposes `/v1/chat/completions`, not OpenAI's Responses API):
  `OpenAIChatModel(model.model_id, provider=OpenAIProvider(base_url=settings.digitalocean_inference_base_url, api_key=os.getenv("DIGITALOCEAN_INFERENCE_API_KEY") or ""))`.
  Settings: return `None` (no reasoning-effort/thinking config) unless a specific
  DO model needs one. **VERIFY** the exact pydantic-ai class name for the chat
  model in the installed version (`OpenAIChatModel` vs `OpenAIModel`) —
  `python -c "import pydantic_ai.models.openai as m; print(dir(m))"`.

Keep top-level imports (no inline imports) in this new module.

### 4. Refactor chat_agent to reuse the router (small, optional-but-preferred)
So there is one router, make `chat_agent._build_model()` delegate for catalog
models while preserving the current default behavior:
- Keep `DEFAULT_MODEL`/`CHAT_AGENT_MODEL` working exactly as today (default id is
  `gemini-3.1-flash-lite`, which is also catalog key `gemini-3-1-flash-lite`).
- Provide a new `chat_agent.build_agent_model(model_id: str)` (or similar) that,
  if `model_id` matches a catalog `model_id`, delegates to `model_router`, else
  falls back to the existing prefix logic. Stage 04 will call this. Do **not**
  change `get_chat_agent()`'s default output in this stage.

## Tests (TDD — write first)
Add `tests/bot/agents/test_model_catalog.py` and
`tests/bot/agents/test_model_router.py`:
- Catalog integrity: every entry has a non-empty `key`/`label`/`model_id`; `key`
  values are unique; `family` ∈ the seven families; total ≤ 24; each of the seven
  families appears at least once.
- `get_model`/`is_valid_model_key` round-trip; unknown key → `None`/`False`.
- Provider assignment: all Gemini entries → `GOOGLE`, all GPT → `OPENAI`, and
  Kimi/GLM/DeepSeek/Gemma/Qwen → `DIGITALOCEAN`.
- Router: for a DO model, `build_model_for` returns a model whose provider is
  configured with `base_url == settings.digitalocean_inference_base_url`. Patch
  `os.environ`/settings and assert the base_url and api_key are threaded through
  (inspect the returned model/provider attributes; if pydantic-ai hides them,
  assert via `unittest.mock` patching `OpenAIProvider` and checking call kwargs).
  For Google/OpenAI models, assert the correct model class is used (patch the
  provider constructors and assert the api_key env var is read).
- These are unit tests, not `@pytest.mark.llm` — no network. Do not hit real
  providers.

Run: `uv run pytest tests/bot/agents/test_model_catalog.py tests/bot/agents/test_model_router.py`

## Acceptance criteria
- New modules `model_catalog.py`, `model_router.py` exist with the API above.
- `Settings` has `digitalocean_inference_base_url`; `.env.example` documents both
  DO vars.
- Full suite green: `uv run pytest` (respecting existing `-m "not llm"` default).
- No runtime behavior change to the live chat agent's default model.
- Before committing: run `semgrep` and `gitleaks` (repo convention). Do not commit
  any real API key.

## Notes / decisions to record in the commit
- DO uses an OpenAI-compatible Chat Completions endpoint; that's why non-Google/
  non-OpenAI families route through `OpenAIChatModel` with a `base_url`.
- The catalog `key` is the stable persisted identifier; `model_id` is the wire id
  and may be re-verified/updated without a migration.
