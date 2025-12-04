# LLM Model Configuration

This project supports flexible LLM model configuration using environment variables. You can switch between different providers (OpenAI, Google Gemini, Anthropic Claude) without code changes.

## Model Types

The project uses three model types for different purposes:

| Model Type | Environment Variable | Default | Purpose |
|------------|---------------------|---------|---------|
| `fast` | `LLM_FAST_MODEL` | `gemini/gemini-2.5-flash-lite` | Quick, cheap operations (most agents) |
| `medium` | `LLM_MEDIUM_MODEL` | `claude-haiku-4-5-20251001` | Higher quality reasoning (forum agent) |
| `judge` | `LLM_JUDGE_MODEL` | `gemini/gemini-2.5-flash-lite` | Evaluation/testing |

## API Keys Required

Add the appropriate API key to your `.env` file:

```bash
# For OpenAI models (GPT-5, GPT-4, etc.)
OPENAI_API_KEY=your_openai_api_key

# For Google Gemini models
GEMINI_API_KEY=your_gemini_api_key

# For Anthropic Claude models
ANTHROPIC_API_KEY=your_anthropic_api_key
```

## Usage Examples

### Switch Fast Model

```bash
# Use GPT-5 Nano for quick operations
LLM_FAST_MODEL=gpt-5-nano-2025-08-07 uv run python -m smarter_dev.bot.client

# Use Gemini 2.5 Flash for quick operations
LLM_FAST_MODEL=gemini/gemini-2.5-flash uv run python -m smarter_dev.bot.client
```

### Switch Medium Model

```bash
# Use Claude Sonnet for complex reasoning
LLM_MEDIUM_MODEL=claude-sonnet-4-20250514 uv run python -m smarter_dev.bot.client
```

### Switch Judge Model

```bash
# Use GPT-5 Nano for LLM-as-judge tests
LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07 uv run python test_mention_agent.py --llm-only

# Use all GPT-5 for testing
LLM_FAST_MODEL=gpt-5-nano-2025-08-07 LLM_MEDIUM_MODEL=gpt-5-nano-2025-08-07 LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07 uv run python test_mention_agent.py --llm-only
```

### Persistent Configuration

Add to your shell profile (`.bashrc`, `.zshrc`, etc.):

```bash
export LLM_FAST_MODEL=gemini/gemini-2.5-flash-lite
export LLM_MEDIUM_MODEL=claude-haiku-4-5-20251001
export LLM_JUDGE_MODEL=gemini/gemini-2.5-flash-lite
```

Or create a `.env` file:

```bash
# .env
LLM_FAST_MODEL=gemini/gemini-2.5-flash-lite
LLM_MEDIUM_MODEL=claude-haiku-4-5-20251001
LLM_JUDGE_MODEL=gemini/gemini-2.5-flash-lite
GEMINI_API_KEY=your_gemini_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here
```

## Model Support

### OpenAI Models
- `gpt-5-nano-2025-08-07`
- `gpt-4o`
- `gpt-4o-mini`
- Any other OpenAI model identifier

### Google Gemini Models
- `gemini/gemini-2.5-flash-lite`
- `gemini/gemini-2.5-flash`
- `gemini/gemini-2.0-flash-lite`
- `gemini/gemini-2.0-flash`

### Anthropic Claude Models
- `claude-haiku-4-5-20251001`
- `claude-sonnet-4-20250514`
- Any other Claude model identifier

### Model Format
Models should be specified using the DSPy format:
- OpenAI: `gpt-model-name` or `openai/gpt-model-name`
- Gemini: `gemini/model-name`
- Claude: `claude-model-name`

## Agent Model Usage

| Agent | Model Type | Purpose |
|-------|------------|---------|
| HelpAgent | `fast` | Quick help responses |
| TLDRAgent | `fast` | Channel summarization |
| StreakAgent | `fast` | Streak celebration messages |
| AoCThreadAgent | `fast` | Advent of Code thread messages |
| ForumMonitorAgent | `medium` | Forum post analysis and response |
| MentionAgent | `judge` | Mention handling with evaluation |

## Configuration Validation

The system automatically validates configuration and provides helpful error messages:

```bash
# Missing API key
Missing ANTHROPIC_API_KEY in .env for model claude-haiku-4-5-20251001

# Invalid model
Configuration error: Unknown provider for model xyz-123
```

## Logging

The bot logs which model it's using on startup:

```
ForumMonitorAgent using LLM model: claude-haiku-4-5-20251001 (provider: anthropic)
HelpAgent using LLM model: gemini/gemini-2.5-flash-lite (provider: gemini)
```

Test runs also display model information:

```
LLM Models:
   Fast:   gemini/gemini-2.5-flash-lite (env: LLM_FAST_MODEL)
   Medium: claude-haiku-4-5-20251001 (env: LLM_MEDIUM_MODEL)
   Judge:  gemini/gemini-2.5-flash-lite (env: LLM_JUDGE_MODEL)
```

## Implementation

The configuration is centralized in `smarter_dev/llm_config.py`, which provides:

- `get_llm_model(model_type)` - Get configured LLM instance ("fast", "medium", or "judge")
- `get_model_info(model_type)` - Get model metadata
- `validate_model_config(model_type)` - Validate configuration

This ensures consistent model configuration across the entire project.
