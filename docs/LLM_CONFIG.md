# LLM Model Configuration

Every LLM call in the project goes through pydantic-ai. Models come from the
model catalogue (`smarter_dev/shared/model_catalog.py`), and
`build_model_for` in `smarter_dev/shared/model_router.py` turns a catalogue
entry into a configured pydantic-ai model: provider, API key, endpoint and
reasoning settings.

## Bot agents

| Agent | Catalogue key | Where it is set |
|-------|---------------|-----------------|
| Chat agent | `gpt-6-luna` by default | `CHAT_AGENT_MODEL`; per-channel overrides in the admin |
| Conversation compactor | `claude-haiku-5-5` | `CHAT_AGENT_COMPACT_MODEL` |
| HelpAgent (`/help`) | `claude-haiku-5-5` | `UTILITY_MODEL_KEY` in `smarter_dev/bot/agents/utility_model.py` |
| TLDRAgent (`/tldr`) | `claude-haiku-5-5` | `UTILITY_MODEL_KEY` |
| StreakCelebrationAgent | `claude-haiku-5-5` | `UTILITY_MODEL_KEY` |
| Timeout announcement | `claude-haiku-5-5` | `UTILITY_MODEL_KEY` |
| ForumMonitorAgent | `claude-haiku-5-5` | `UTILITY_MODEL_KEY` |
| Moderation triage | `claude-haiku-5-5` | `UTILITY_MODEL_KEY` |
| Proactive agent | `claude-opus-5-5` | `PROACTIVE_AGENT_MODEL` (bot and proactive-agent deployment) |

The six utility agents ran on DSPy until 2026-10 and read `LLM_FAST_MODEL`
(help, tldr, streak, timeout) and `LLM_MEDIUM_MODEL` (forum, moderation).
Both were `gpt-6-luna` everywhere, so they mapped to the `gpt-6-luna` catalogue
entry at its default reasoning level. `LLM_JUDGE_MODEL` was used only by
LLM-as-judge tests. All three variables are gone; to change these agents'
model, change `UTILITY_MODEL_KEY` to another catalogue key.

### Off Luna (2026-10-17, #199)

Chat stays on GPT-6 Luna. Every other use of Luna moved to Claude Haiku 5.5:
the compactors (Discord's `CHAT_AGENT_COMPACT_MODEL` and web Chat's
`chat_settings.compaction_model_key`), the utility agents above, the web
summarizer and the overlong-reply summarizer, the image readers
(`MEDIA_READER_MODEL`; audio stays on Gemini), the image-prompt reviewer, the
member-tier handler author and judge and the gathering agent, the dashboard web
search (`WEB_SEARCH_QUERY_MODEL`, `WEB_SEARCH_ANSWER_MODEL`), the Resources
pipeline, the title agent and the blogging pipeline. Each keeps its env
override; an override must name a catalogue id or a `claude-*` id.

## Voice

Discord voice messages use Gemini TTS directly:

| Environment Variable | Default | Purpose |
|----------------------|---------|---------|
| `VOICE_TTS_MODEL` | `gemini-2.5-flash-preview-tts` | Gemini TTS model for voice messages |
| `VOICE_TTS_VOICE` | `Kore` | Gemini prebuilt TTS voice |
| `VOICE_MAX_INPUT_CHARS` | `800` | Maximum response text sent to TTS |
| `VOICE_WORDS_PER_MINUTE` | `150` | Estimated speaking rate for voice response word budget |
| `VOICE_MAX_DURATION_SECONDS` | `30` | Target maximum spoken duration for voice responses |
| `VOICE_OPUS_BITRATE` | `48k` | Opus bitrate for the Discord voice attachment |

## API keys

`build_model_for` reads keys from the environment (and `.env` locally):

```bash
OPENAI_API_KEY=your_openai_api_key        # OpenAI catalogue models
GEMINI_API_KEY=your_gemini_api_key        # Gemini models and voice TTS
ANTHROPIC_API_KEY=your_anthropic_api_key  # Claude models (CLAUDE_API_KEY also read)
OPENROUTER_API_KEY=your_openrouter_key    # OpenRouter models
```

### Claude

Every Claude model calls Anthropic's API directly: the catalogue's Sonnet 5.5
and Haiku 5.5, and any other `claude-*` id named in config (for example a
`PROACTIVE_AGENT_MODEL` of `claude-opus-5-5`). The key is `ANTHROPIC_API_KEY`,
with `CLAUDE_API_KEY` as a fallback name; in Kubernetes it comes from
`smarter-dev-secrets` key `anthropic-api-key`.

Requests cache the tool definitions, the system prompt and the newest message
with 1-hour breakpoints (`ANTHROPIC_CACHE_SETTINGS` in
`smarter_dev/shared/model_router.py`). Because every breakpoint is 1-hour,
`llm_pricing` prices every direct Claude cache write at the 1-hour rate.
Agents that answer in one request (titles, image reads, summaries, compaction,
the judges) pass `cache_messages=False`: they never read their messages back,
so that breakpoint would only bill the prompt at twice the input rate.

Agents the agent-worker runs through Skrift take a model name rather than a
model; `smarter_dev/shared/skrift_models.py` builds the name
(`anthropic:claude-haiku-5-5`) and the same settings as plain dicts. They read
`ANTHROPIC_API_KEY` only.

## Comparing models for moderation

`tests/bot/agents/test_moderation_harness.py` runs the moderation scenarios
against several catalogue models with the bot's prompts and mock tools:

```bash
uv run python tests/bot/agents/test_moderation_harness.py --models gpt-6-luna gemini-flash
```
