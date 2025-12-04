# Mention Agent Contextual Filtering Tests

This directory contains comprehensive tests for the Discord bot mention agent's contextual content filtering system.

## Test Files

- `tests/bot/test_mention_agent_llm_judge.py` - Comprehensive LLM-as-judge test suite
- `test_mention_agent.py` - Test runner script

## Test Categories

### Unit Tests (Structure validation)
Basic tests that validate agent structure and configuration without API calls:

```bash
# Run unit tests only
uv run python test_mention_agent.py

# Or directly with pytest 
uv run python -m pytest tests/bot/test_mention_agent_llm_judge.py -m "not llm" --no-cov
```

**Basic structure validation:**
- ✅ Agent instantiation
- ✅ Signature filtering instructions  
- ✅ Discord message structure

### LLM Evaluation Tests (Real API calls)
Uses Gemini 2.5 Flash Lite to evaluate the quality of contextual filtering decisions:

```bash
# Run LLM evaluation tests (uses API credits)
uv run python test_mention_agent.py --llm-only

# Run all tests including LLM evaluation
uv run python test_mention_agent.py --with-llm
```

**Requirements:**
- API key in `.env` file: `GEMINI_API_KEY` (default) or `OPENAI_API_KEY` (for OpenAI models)
- API credits will be consumed
- Tests take longer to run

**Model Selection:**
The project supports switching LLM models system-wide using environment variables:

- `LLM_FAST_MODEL`: Fast model for quick operations (default: `gemini/gemini-2.5-flash-lite`)
- `LLM_MEDIUM_MODEL`: Medium intelligence model for complex reasoning (default: `claude-haiku-4-5-20251001`)
- `LLM_JUDGE_MODEL`: Judge evaluation model (default: `gemini/gemini-2.5-flash-lite`)

  ```bash
  # Use GPT-5 Nano for all models
  LLM_FAST_MODEL=gpt-5-nano-2025-08-07 LLM_MEDIUM_MODEL=gpt-5-nano-2025-08-07 LLM_JUDGE_MODEL=gpt-5-nano-2025-08-07 uv run python test_mention_agent.py --llm-only

  # Use GPT-5 Nano for fast model, keep defaults for medium and judge
  LLM_FAST_MODEL=gpt-5-nano-2025-08-07 uv run python -m smarter_dev.bot.client

  # Use different Gemini models
  LLM_FAST_MODEL=gemini/gemini-2.5-flash LLM_JUDGE_MODEL=gemini/gemini-2.5-flash uv run python test_mention_agent.py --llm-only
  ```

**Comprehensive scenarios evaluated:**
- ✅ Regular programming conversations (should engage normally)
- ✅ Respectful tech policy discussions (should engage normally)
- ✅ Casual political mentions (should redirect gracefully) 
- ✅ Partisan political arguments (should redirect)
- ✅ Persistent agenda-pushing (should skip after redirects)
- ✅ Aggressive/hostile behavior (should skip)
- ✅ Light controversial humor (should redirect with matching tone)
- ✅ Mental health crises (should skip - not appropriate for bot)
- ✅ Community behavior callouts (should respond appropriately)
- ✅ Empty conversation context (graceful greetings)
- ✅ Mixed content (prioritize recent context)

**Evaluation criteria:**
- **Appropriateness**: Was the skip/respond/redirect decision correct?
- **Quality**: If responded, was the response helpful and well-crafted?
- **Community Tone**: Does response maintain welcoming developer vibe?
- **Context Awareness**: Did bot consider conversation history and patterns?
- **Effectiveness**: Does response handle the situation as intended?

## Understanding Test Results

### Expected Behavior

**SHOULD ENGAGE NORMALLY:**
- Programming discussions and technical questions
- Regular community chat and bot feature questions  
- **Respectful tech policy discussions** (GDPR, privacy laws, etc.)
- **Professional conversations about tech regulation**
- **Civil discussions where opinions are held loosely**

**SHOULD REDIRECT (not skip):**
- Partisan political arguments or heated ideological debates
- First-time casual mentions of politics without tech relevance
- Light humor or casual political references
- Philosophical discussions that drift from tech

**SHOULD SKIP (empty response):**
- Persistent pushing after redirect attempts
- Aggressive, hostile, inflammatory behavior
- Repeated guideline violations after warnings
- Serious mental health crises
- Obvious bait designed to cause arguments

### Structure vs LLM Evaluation Tests

- **Structure tests**: Basic validation of agent instantiation and configuration (no API calls)
- **LLM evaluation tests**: Real Gemini API calls with LLM-as-judge evaluation of contextual filtering behavior

All tests that use the LLM are properly marked with `@pytest.mark.llm` to exclude them from regular CI/CD test runs.

## Configuration

### pytest.ini Configuration
The project's `pyproject.toml` includes:

```toml
markers = [
    "llm: marks tests that use LLM APIs (expensive, skip by default with '-m \"not llm\"')",
]
```

### Running in CI/CD
To exclude LLM tests in CI pipelines:

```bash
# Skip LLM tests (default behavior)
pytest -m "not llm"

# Only run unit tests for mention agent
pytest tests/bot/test_mention_agent_llm_judge.py -m "not llm"
```

## Contextual Filtering Logic

The mention agent now uses contextual filtering that:

1. **Analyzes conversation history** to determine user patterns
2. **Gives benefit of the doubt** on first interactions
3. **Gracefully redirects** sensitive topics to programming discussions  
4. **Only skips responses** for persistent problems or inappropriate content
5. **Maintains community spirit** through humor and helpful redirection
6. **Calls out bad behavior** when appropriate, like a good community member

This represents a significant improvement over the previous strict content blocking approach.