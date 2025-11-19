# Evaluation Framework Quick Start

## What This Is

A comprehensive evaluation system to compare **Gemini 2.5 Flash**, **Gemini 2.5 Flash Lite**, **Claude Haiku 4.5**, and **Claude Sonnet 4.5** for the mention agent's planning capabilities.

**✨ NEW: Fast Planning-Only Evaluation** - Tests planning abilities without executing actions. **12x faster** than full execution!

## 30 Test Scenarios

✅ **Tool Usage** (15 scenarios): Web search, URL analysis, code review, typing indicators, etc.
✅ **Conversation** (10 scenarios): Greetings, multi-user discussions, context awareness, etc.
✅ **Edge Cases** (5 scenarios): Rate limits, cost awareness, minimal context, etc.

## Quick Commands (Planning-Only - Recommended!)

### 1. Smoke Test (2 models, 3 scenarios, ~30 seconds) ⚡

```bash
pytest tests/evaluation/test_planning_comparison.py::test_planning_smoke -v -s -m llm --no-cov
```

Best for: Validating the framework works quickly

### 2. Single Scenario Test (~20 seconds) ⚡

```bash
EVAL_SCENARIO="01_simple_web_search" \
pytest tests/evaluation/test_planning_comparison.py::test_planning_single_scenario -v -s -m llm --no-cov
```

Best for: Testing a specific scenario across all models

### 3. Full Comparison (4 models, 30 scenarios, ~10 minutes) ⚡

```bash
pytest tests/evaluation/test_planning_comparison.py::test_planning_full -v -s -m llm --no-cov
```

Best for: Complete planning evaluation (costs ~$0.60)

### 4. Category Test (e.g., tool_usage, ~5 minutes) ⚡

```bash
EVAL_CATEGORY="tool_usage" \
pytest tests/evaluation/test_planning_comparison.py::test_planning_category -v -s -m llm --no-cov
```

Best for: Testing specific categories

**Note**: Planning-only tests show:
- Progress counter ([1/120], [2/120], etc.)
- Which scenario and model is running
- Plan generation and evaluation steps
- Token usage, quality scores, time and cost per run
- Mini summaries after each scenario

## Why Planning-Only?

- ⚡ **12x faster**: 2-10s per eval (vs 30-120s)
- 💰 **2x cheaper**: Single LLM call per eval
- 🎯 **More focused**: Tests planning, not execution
- 🐛 **Easier to debug**: Simple, clear output
- ✅ **Same metrics**: All 5 quality scores + tool accuracy

See `PLANNING_VS_EXECUTION.md` for details.

## What Gets Measured

1. **Quality** (1-10 scale): LLM judge evaluates appropriateness, quality, tone, context awareness, effectiveness
2. **Cost**: Token usage and estimated $ per run
3. **Latency**: Time to first action and total execution time
4. **Tool Accuracy**: Precision, recall, F1 score for tool selection

## Output

Results saved to `tests/evaluation/results/`:

- **HTML Report**: Interactive charts (open in browser)
- **Markdown Report**: Text summary with recommendations
- **JSON Report**: Raw data for analysis
- **Scenario Breakdown**: Per-scenario comparison

## Cost Estimates

- **Smoke Test**: ~$0.01
- **Single Scenario**: ~$0.03
- **Full Evaluation**: ~$1.04

## Example Usage

```bash
# 1. Quick validation
pytest tests/evaluation/test_model_comparison.py::test_quick_smoke -v -s

# 2. Test tool usage scenarios only
EVAL_CATEGORY="tool_usage" \
pytest tests/evaluation/test_model_comparison.py::test_category -v -s

# 3. Test Gemini Flash vs Haiku
EVAL_MODELS="gemini/gemini-2.0-flash-exp,anthropic/claude-3-5-haiku-20241022" \
pytest tests/evaluation/test_model_comparison.py::test_compare_custom_models -v -s

# 4. Full evaluation for decision-making
pytest tests/evaluation/test_model_comparison.py::test_compare_all_models -v -s
```

## Next Steps

1. Run smoke test to validate: `pytest tests/evaluation/test_model_comparison.py::test_quick_smoke -v -s`
2. Review the HTML report in `tests/evaluation/results/smoke_test.html`
3. If satisfied, run full evaluation: `pytest tests/evaluation/test_model_comparison.py::test_compare_all_models -v -s`
4. Use results to decide on mention agent architecture

## Need Help?

See full documentation: `tests/evaluation/README.md`
