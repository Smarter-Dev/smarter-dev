# LLM Model Configuration

This project supports flexible LLM model configuration using environment variables. You can switch between different providers (OpenAI, Google Gemini, etc.) without code changes.

## Environment Variables

| Variable | Purpose | Default | Examples |
|----------|---------|---------|----------|
| `LLM_MODEL` | Main bot model | `gemini/gemini-2.0-flash-lite` | `gpt-5-nano-2025-08-07`, `gemini/gemini-2.5-flash` |
| `LLM_JUDGE_MODEL` | Test evaluation model | `gemini/gemini-2.5-flash-lite` | `gpt-5-nano-2025-08-07`, `gemini/gemini-2.5-flash` |

## API Keys Required

Add the appropriate API key to your `.env` file:

```bash
# For OpenAI models (GPT-5, GPT-4, etc.)
OPENAI_API_KEY=your_openai_api_key

# For Google Gemini models
GEMINI_API_KEY=your_gemini_api_key

# For Anthropic Claude models (if supported)
ANTHROPIC_API_KEY=your_anthropic_api_key
```

## Usage Examples

### Switch Main Bot Model

```bash
# Use GPT-5 Nano for the Discord bot
LLM_MODEL=gpt-5-nano-2025-08-07 uv run python -m smarter_dev.bot.client

# Use Gemini 2.5 Flash for the Discord bot
LLM_MODEL=gemini/gemini-2.5-flash uv run python -m smarter_dev.bot.client
```

### Switch Test Judge Model

```bash
# Use GPT-5 Nano for LLM-as-judge tests
LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07 uv run python test_mention_agent.py --llm-only

# Use both GPT-5 for bot and judge
LLM_MODEL=gpt-5-nano-2025-08-07 LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07 uv run python test_mention_agent.py --llm-only
```

### Persistent Configuration

Add to your shell profile (`.bashrc`, `.zshrc`, etc.):

```bash
export LLM_MODEL=gpt-5-nano-2025-08-07
export LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07
```

Or create a `.env` file:

```bash
# .env
LLM_MODEL=gpt-5-nano-2025-08-07
LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07
OPENAI_API_KEY=your_api_key_here
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

### Model Format
Models should be specified using the DSPy format:
- OpenAI: `gpt-model-name` or `openai/gpt-model-name`
- Gemini: `gemini/model-name`
- Other providers: Follow DSPy conventions

## Configuration Validation

The system automatically validates configuration and provides helpful error messages:

```bash
# Missing API key
❌ Missing OPENAI_API_KEY in .env for model gpt-5-nano-2025-08-07

# Invalid model
❌ Configuration error: Unknown provider for model xyz-123
```

## Logging

The bot logs which model it's using on startup:

```
🤖 Bot using LLM model: gpt-5-nano-2025-08-07 (provider: openai)
```

Test runs also display model information:

```
💡 LLM Models:
   Main bot: gpt-5-nano-2025-08-07 (env: LLM_MODEL)
   Judge:    gpt-5-nano-2025-08-07 (env: LLM_JUDGE_MODEL)
```

## Implementation

The configuration is centralized in `smarter_dev/llm_config.py`, which provides:

- `get_llm_model(model_type)` - Get configured LLM instance
- `get_model_info(model_type)` - Get model metadata  
- `validate_model_config(model_type)` - Validate configuration

This ensures consistent model configuration across the entire project.