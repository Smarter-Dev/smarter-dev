# Mention Agent Model Evaluation Framework

Comprehensive evaluation suite for comparing different LLM models on mention agent planning and decision-making tasks.

## Overview

This framework evaluates how well different language models perform as the core planning/decision-making component of the mention agent. It tests models across:

- **4 Models**: Gemini 2.5 Flash, Gemini 2.5 Flash Lite, Claude Haiku 4.5, Claude Sonnet 4.5
- **30 Scenarios**: Covering tool usage, conversation patterns, and edge cases
- **4 Metric Categories**: Quality, Cost, Latency, Tool Usage Accuracy

## Quick Start

### Run Complete Evaluation

Compare all 4 models across all 30 scenarios:

```bash
pytest tests/evaluation/test_model_comparison.py::test_compare_all_models -v -s
```

This will:
1. Run 120 evaluations (4 models × 30 scenarios)
2. Generate comparison reports in `tests/evaluation/results/`
3. Take approximately 30-60 minutes depending on API speeds

### Quick Smoke Test

Test 2 models on 3 scenarios for quick validation:

```bash
pytest tests/evaluation/test_model_comparison.py::test_quick_smoke -v -s
```

## Evaluation Modes

### 1. Full Comparison

```bash
pytest tests/evaluation/test_model_comparison.py::test_compare_all_models -v -s
```

Evaluates all models on all scenarios. Results in `results/full_comparison.*`

### 2. Custom Models

```bash
EVAL_MODELS="gemini/gemini-2.0-flash-exp,anthropic/claude-3-5-haiku-20241022" \
pytest tests/evaluation/test_model_comparison.py::test_compare_custom_models -v -s
```

Test a custom set of models. Results in `results/custom_comparison.*`

### 3. Single Scenario

```bash
EVAL_SCENARIO="01_simple_web_search" \
pytest tests/evaluation/test_model_comparison.py::test_single_scenario -v -s
```

Test all models on one scenario for debugging. Results in `results/scenario_01_simple_web_search.*`

### 4. Single Model

```bash
EVAL_MODEL="gemini/gemini-2.0-flash-exp" \
pytest tests/evaluation/test_model_comparison.py::test_single_model -v -s
```

Test one model on all scenarios. Results in `results/model_gemini_gemini-2.0-flash-exp.*`

### 5. Category Testing

```bash
EVAL_CATEGORY="tool_usage" \
pytest tests/evaluation/test_model_comparison.py::test_category -v -s
```

Test all models on scenarios from a specific category. Results in `results/category_tool_usage.*`

Available categories:
- `tool_usage` - Tool selection and orchestration (12 scenarios)
- `conversation` - Conversation handling patterns (10 scenarios)
- `edge_cases` - Edge cases and decision quality (8 scenarios)

## Architecture

### Components

```
tests/evaluation/
├── scenarios/              # 30 YAML scenario definitions
├── results/                # Generated reports (git-ignored)
├── metrics_tracker.py      # Metrics collection and aggregation
├── llm_judge.py           # LLM-as-judge quality evaluation
├── model_comparison_runner.py  # Core evaluation runner
├── report_generator.py    # Report generation with visualizations
└── test_model_comparison.py    # Pytest test suite
```

### Metrics Collected

#### 1. **Quality Metrics** (LLM-as-judge, 1-10 scale)
- **Appropriateness**: Was the decision to respond/skip/redirect appropriate?
- **Quality**: If responded, was it helpful and well-crafted?
- **Community Tone**: Does it maintain welcoming developer vibe?
- **Contextual Awareness**: Did it consider conversation history?
- **Effectiveness**: Does it handle the situation as intended?

#### 2. **Cost Metrics**
- Input tokens
- Output tokens
- Estimated cost (USD)
- Total tokens

#### 3. **Latency Metrics**
- Time to first action
- Total execution time

#### 4. **Tool Usage Accuracy**
- Precision: Of tools used, how many were expected?
- Recall: Of expected tools, how many were used?
- F1 Score: Harmonic mean of precision/recall
- Sequence Accuracy: How well tool ordering matches expectations?

## Test Scenarios

### Tool Usage Patterns (12 scenarios)

| ID | Scenario | Tests |
|----|----------|-------|
| 01 | Simple web search | Instant answer lookup |
| 02 | Technical question | In-depth technical response |
| 03 | URL analysis | Document/API analysis |
| 04 | Multi-step research | Complex research workflow |
| 06 | Reaction only | Emoji reactions |
| 08 | Tool chaining | Multi-tool workflows |
| 13 | Typing indicator | Long operation management |
| 14 | Wait for messages | Smart debouncing |
| 15 | Reply threading | Message threading |
| 21 | Comparison question | Research + synthesis |
| 22 | Debugging help | Technical debugging |
| 23 | Code review | Code analysis |
| 26 | List reactions | Tool discovery |
| 27 | Architecture explanation | Technical depth |
| 28 | News question | Current events |
| 29 | Wait duration | Simple waiting |

### Conversation Patterns (10 scenarios)

| ID | Scenario | Tests |
|----|----------|-------|
| 05 | Greeting casual | Simple greetings |
| 07 | Multi-user complex | Strategic planning needed |
| 09 | Opinion question | Personality expression |
| 10 | Context dependent | Using conversation history |
| 11 | Multi-turn conversation | Continuation handling |
| 16 | Ambiguous query | Clarification |
| 24 | Rapid-fire messages | Debounce strategy |
| 25 | Mixed casual/technical | Tone balancing |
| 30 | Fetch new messages | Conversation monitoring |

### Edge Cases & Decision Quality (8 scenarios)

| ID | Scenario | Tests |
|----|----------|-------|
| 12 | No tools needed | Direct response |
| 17 | Rate limit awareness | Cache checking |
| 18 | Cost conscious | Avoiding expensive tools |
| 19 | Stop monitoring | Graceful exit |
| 20 | Minimal context | Handling empty mentions |

## Report Formats

### HTML Report
Interactive charts with Chart.js showing:
- Cost comparison
- Speed comparison
- Quality scores
- Tool accuracy
- Detailed metrics per model

**Location**: `results/full_comparison.html`

### Markdown Report
Text-based summary with:
- Executive summary table
- Detailed metrics per model
- Recommendations

**Location**: `results/full_comparison.md`

### JSON Report
Raw data for programmatic access:
- All individual runs
- Complete metrics
- Summary statistics

**Location**: `results/full_comparison.json`

### Scenario Breakdown
Per-scenario comparison showing each model's performance on each scenario.

**Location**: `results/full_comparison_scenarios.md`

## Adding New Scenarios

1. Create a YAML file in `scenarios/`:

```yaml
id: "31_new_scenario"
category: "tool_usage"
description: "What this scenario tests"

channel_name: "general"
channel_description: "General discussion channel"

conversation:
  - timestamp: "10:00:00"
    user: "Alice"
    user_id: "111111111"
    content: "@SmarterDev your question here"

recent_search_queries: []
messages_remaining: 50
is_continuation: false

expected_behavior:
  expected_tools: ["send_message", "search_web"]
  expected_sequence: ["send_message", "search_web", "send_message"]
  expected_response_type: "casual_answer"
  description: "What the agent should do"
  key_points:
    - "First key point"
    - "Second key point"
```

2. Run the evaluation:

```bash
EVAL_SCENARIO="31_new_scenario" \
pytest tests/evaluation/test_model_comparison.py::test_single_scenario -v -s
```

## Model Configuration

### Tested Models

| Model | Provider | Model ID | Released | Use Case |
|-------|----------|----------|----------|----------|
| Gemini 2.5 Flash | Google | `gemini/gemini-2.5-flash` | - | Balanced performance |
| Gemini 2.5 Flash Lite | Google | `gemini/gemini-2.5-flash-lite` | - | Current baseline, fast & cheap |
| Claude Haiku 4.5 | Anthropic | `anthropic/claude-haiku-4-5-20251001` | Oct 1, 2025 | Fast, cost-effective |
| Claude Sonnet 4.5 | Anthropic | `anthropic/claude-sonnet-4-5-20250929` | Sep 29, 2025 | Highest quality |

### Model Naming

Use DSPy/LiteLLM format with `anthropic/` prefix:
- **Gemini 2.5**: `gemini/gemini-2.5-flash`, `gemini/gemini-2.5-flash-lite`
- **Claude Haiku 4.5**: `anthropic/claude-haiku-4-5-20251001` (dated) or `anthropic/claude-haiku-4-5` (alias)
- **Claude Sonnet 4.5**: `anthropic/claude-sonnet-4-5-20250929` (dated) or `anthropic/claude-sonnet-4-5` (alias)

**Note**: Using dated versions (e.g., `20251001`) is recommended for production consistency. Aliases automatically point to the latest snapshot but may change over time.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LLM_JUDGE_MODEL` | Model for quality judging | `gemini/gemini-2.5-flash` |
| `EVAL_MODELS` | Comma-separated model list | All 4 models |
| `EVAL_SCENARIO` | Single scenario to test | `01_simple_web_search` |
| `EVAL_MODEL` | Single model to test | First model |
| `EVAL_CATEGORY` | Scenario category to test | `tool_usage` |

## Cost Estimates

Approximate costs for full evaluation (4 models × 30 scenarios = 120 runs):

| Model | Pricing (per MTok) | Est. Cost per Run | Total Cost (30 scenarios) |
|-------|-------------------|-------------------|---------------------------|
| Gemini 2.5 Flash Lite | $0.05 / $0.20 | ~$0.001 | ~$0.03 |
| Gemini 2.5 Flash | $0.075 / $0.30 | ~$0.002 | ~$0.06 |
| Claude Haiku 4.5 | $1.00 / $5.00 | ~$0.005 | ~$0.15 |
| Claude Sonnet 4.5 | $3.00 / $15.00 | ~$0.02 | ~$0.60 |

**Full evaluation**: ~$0.84 + LLM judge costs (~$0.20) = **~$1.04 total**

*Pricing as of January 2025. Input/Output tokens shown.*

## Performance Tips

1. **Parallel Execution**: Run categories in parallel using separate terminals
2. **Skip Expensive Models**: Test with cheaper models first, add Sonnet later
3. **Incremental Testing**: Use single scenario/model tests during development
4. **Cache Results**: JSON files can be reprocessed without re-running evaluations

## Troubleshooting

### "No module named 'tests.evaluation'"

Run from project root:
```bash
cd /Users/zechariahzimmerman/Projects/Smarter\ Dev/smarter-dev
pytest tests/evaluation/test_model_comparison.py::test_quick_smoke -v -s
```

### API Rate Limits

Add delays between runs or reduce concurrent scenarios:
```python
# In model_comparison_runner.py
await asyncio.sleep(1)  # Add after each run
```

### Out of Memory

Process scenarios in batches:
```bash
# Run categories separately
EVAL_CATEGORY="tool_usage" pytest tests/evaluation/test_model_comparison.py::test_category -v -s
EVAL_CATEGORY="conversation" pytest tests/evaluation/test_model_comparison.py::test_category -v -s
```

## Next Steps

After running evaluations:

1. **Review HTML Report**: Open `results/full_comparison.html` in browser
2. **Check Scenario Breakdown**: Identify scenarios where models differ significantly
3. **Analyze Quality Scores**: Look at judge reasoning for low scores
4. **Cost Analysis**: Calculate cost implications of model selection
5. **Make Decision**: Choose model based on your priorities (cost vs quality vs speed)

## Architecture Decisions

This evaluation framework was designed to help decide how to architect the mention agent:

- **Current**: Gemini 2.5 Flash Lite (router) + Claude Haiku (planning/technical)
- **Options**: Single model for all tasks, or different models for different roles
- **Goal**: Find optimal balance of cost, quality, speed, and accuracy

Use these results to inform architectural changes to the mention agent.
