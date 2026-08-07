# Stage 02 — Teach the chat prompts to use standard LaTeX delimiters

## Goal of this stage

Flip the two chat-agent prompts so models write math with the **standard LaTeX
delimiters** the parser now recognises (`$$…$$`, `\[…\]`, `\(…\)`, `$…$`) instead of
the ```` ```latex ```` fenced-block convention, and confirm end-to-end dispatch still
renders those expressions as images. This is the payoff of Stage 01: the parser
already accepts delimiters, so this stage changes what the models are told to
produce.

Depends on Stage 01 (delimiter detection in
`smarter_dev/bot/agents/latex_blocks.py`) being committed. If Stage 01 is present,
this stage is safe on its own; if for any reason it is not, do Stage 01 first.

## Why

The fenced-block instruction is the part small models get wrong. Standard
delimiters are what they emit naturally, so instructing delimiters raises the rate
of correctly-rendered math. Stage 01 made the parser tolerant of both, so this
prompt change cannot break rendering — worst case a model still emits a fence, which
still renders.

## Files to change

1. `smarter_dev/bot/agents/prompts/chat_agent.md` — line ~35, the LaTeX rule.
2. `smarter_dev/bot/agents/prompts/writer_agent.md` — line ~17, the same rule
   (kept in sync between the single-stage chat agent and the two-stage writer).

### Current text (both files, identical wording)

> - When mathematical notation is clearer than prose, put each complete display
>   equation in its own fenced block whose language is exactly `latex`. Put
>   explanations outside the fence. Never use `$`, `$$`, `\(`, or `\[` LaTeX
>   delimiters outside that fence. Use ordinary Markdown code fences for source
>   code.

### Replace with (same guidance, inverted to delimiters)

Write a rule that instructs the model to:

- Use standard LaTeX delimiters for math: `$$…$$` or `\[…\]` for a complete display
  equation, `\(…\)` or `$…$` for a short inline expression. Each delimited
  expression is rendered as an image, so **prefer display delimiters (`$$…$$` /
  `\[…\]`) for anything meant to stand on its own**, and keep inline `$…$`/`\(…\)`
  for small in-sentence symbols only.
- Put prose/explanations outside the delimiters.
- Use ordinary Markdown code fences (```` ``` ````) for **source code**, not for math.
- Not escape or avoid `$`; write literal currency as `\$` so it is not mistaken for
  math (matches the parser's `\$` handling and single-`$` currency guard).

Keep the wording tight and in the same terse bullet style as the surrounding
prompt. Do not add new sections; just replace the one bullet in each file. Keep the
two files' wording identical to each other.

## Assumptions

Recorded here (thread message-5); revision loop absorbs corrections.

- **A1** — The ```` ```latex ```` fence path stays supported by the parser (Stage 01),
  so this prompt change is purely additive to reliability; a model that still
  fences will still render. (Q1.)
- **A2** — Inline expressions render as their own image message in source order.
  The prompt mitigates sentence fragmentation by steering standalone math to
  display delimiters. (Q2.)

## Verification / tests

The chat-engine dispatch (`_send_fenced_response`, `has_latex_section` gate in
`_apply_output`) already consumes parser output generically and needs **no code
change** — Stage 01 kept the section contract identical. Confirm the seam with an
integration-level check in `tests/bot/services/test_chat_engine_latex.py`:

- Add a test that `_send_fenced_response` on a delimiter message such as
  `"area is $$\\pi r^2$$ done"` sends text, then the rendered PNG (renderer
  mocked), then text — in order — proving the new detection flows through dispatch
  unchanged.
- Keep all existing fenced-block dispatch tests passing.

Prompt files have no unit tests; the changed bullet is validated by review against
the wording above. If the repo has a prompt-snapshot or lint check, update the
snapshot.

Run:
- `uv run pytest tests/bot/services/test_chat_engine_latex.py tests/bot/agents/test_latex_blocks.py`

## Definition of done

- Both prompt bullets instruct standard delimiters and stop forbidding `$`/`\(`/
  `\[`; wording identical between the two files and in the surrounding style.
- The added dispatch test passes and all prior LaTeX tests still pass.
- No behavioural change needed in `chat_engine.py` or `latex_renderer.py`; if any
  edit there was required, explain why in the commit body.
- Run `semgrep` and `gitleaks` before committing.

## Commit

Conventional commit, e.g.
`feat(chat): prompt models for standard LaTeX delimiters over fences`.
