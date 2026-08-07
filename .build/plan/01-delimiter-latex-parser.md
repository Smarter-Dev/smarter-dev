# Stage 01 — Detect delimiter-based LaTeX expressions in the section parser

## Goal of this stage

Teach the Discord chat bot's response parser to recognise LaTeX expressions
written with **standard math delimiters** (`$$…$$`, `\[…\]`, `\(…\)`, `$…$`) and
break the message at each expression, in addition to the existing ```` ```latex ````
fences. Every recognised expression becomes a render break that the *existing*
MathJax renderer and Discord dispatch turn into a PNG image, exactly as fenced
blocks are handled today.

This stage changes only `smarter_dev/bot/agents/latex_blocks.py` and its tests.
The renderer (`latex_renderer.py`), the chat-engine dispatch
(`_send_fenced_response` in `chat_engine.py`), and the prompts are **not** touched
here — they consume the parser's output unchanged, so the codebase stays fully
working after this stage. The prompts are updated in Stage 02.

## Why

Smaller models struggle to reliably emit the exact ```` ```latex ```` fenced-block
convention. Standard LaTeX delimiters are what models naturally produce. By
detecting delimiters and breaking the message at each expression, the bot can
insert the same rendered image without depending on the fence format.

## Background — the current pipeline (read this first)

`smarter_dev/bot/agents/latex_blocks.py` today exposes:

- `TextSection(text: str)` — ordinary Discord markdown to send as text.
- `LatexSection(source: str, original: str)` — one equation: `source` is the raw
  TeX handed to the renderer; `original` is the exact substring of the reply
  (delimiters included) used as a lossless text fallback if rendering fails.
- `ResponseSection = TextSection | LatexSection`.
- `split_latex_sections(message: str) -> list[ResponseSection]` — walks the
  message line by line, pulling out ```` ```latex ```` fences (info string exactly
  `latex`, case-insensitive) as `LatexSection`, leaving everything else — including
  other fenced code blocks — as `TextSection`. Adjacent text is coalesced.
- `has_latex_section(message: str) -> bool` — true if any `LatexSection` results.
- Module constants: `MAX_LATEX_BLOCKS = 5`, `MAX_LATEX_SOURCE_CHARS = 1_800`.

Downstream consumers (do NOT change them here; know how they behave):

- `chat_engine.ChannelEngine._apply_output` calls `has_latex_section(body.message)`
  to decide whether to route through `_send_fenced_response`.
- `_send_fenced_response(message, reply_to)` calls `split_latex_sections`, then for
  each section sends `TextSection.text` as a Discord message or renders
  `LatexSection.source` to a PNG (falling back to sending `LatexSection.original`
  as text on any render failure). Order is preserved; only the first delivered
  message is anchored as a reply.

Because this stage keeps the section names and the two public function
signatures identical, downstream code keeps working with zero changes.

## What to build

Extend `split_latex_sections` so that, in addition to ```` ```latex ```` fences, it
recognises LaTeX expressions delimited by:

| Delimiter        | Kind    | Example              |
|------------------|---------|----------------------|
| `$$ … $$`        | display | `$$E = mc^2$$`       |
| `\[ … \]`        | display | `\[ E = mc^2 \]`     |
| `\( … \)`        | inline  | `\( x^2 \)`          |
| `$ … $`          | inline  | `$x^2$`              |

Each recognised expression is emitted as a `LatexSection` in source order, with:
- `source` = the TeX between the delimiters, `.strip()`-ed (delimiters removed).
- `original` = the exact matched substring **including** its delimiters, so the
  text fallback is lossless.
Text before/between/after expressions becomes `TextSection`(s), coalesced.

### Detection rules (keep them precise and conservative)

1. **Fenced ```` ```latex ```` blocks keep working unchanged.** They take priority:
   content inside any fenced code block (```` ``` ````/`~~~`, any language,
   including ```` ```latex ````) is never scanned for `$`/`\(`/`\[` delimiters. The
   existing line-oriented fence walk already isolates fenced regions — preserve
   that, and run delimiter detection only over the non-fenced text spans.

2. **Delimiter precedence within a text span:** scan left to right; at each
   position try to match, in this order, `$$`, `\[`, `\(`, then single `$`. This
   makes `$$…$$` win over `$…$`. A `\[` must be closed by `\]`; `\(` by `\)`; `$$`
   by `$$`; `$` by the next single `$`.

3. **Backslash-escaped dollar signs are literal.** `\$` is never a delimiter (it is
   a literal dollar in the text). Preserve `\$` verbatim in the surrounding
   `TextSection`.

4. **Single-`$` currency guard.** A `$…$` pair is treated as math **only** when all
   of these hold; otherwise the opening `$` is literal text and scanning continues
   after it:
   - the content between the two `$` is non-empty after stripping whitespace;
   - the pair does not span a blank line (no `\n\n` inside — inline math is
     single-paragraph);
   - it is not plain currency: reject when the content is only digits, spaces,
     commas, and periods (e.g. `$5`, `$1,000.00`). Content containing a TeX-ish
     character (letter, `\`, `^`, `_`, `{`, `}`, `+`, `=`, `/`, etc.) is accepted.
   `$$`, `\[`, `\(` have no currency ambiguity and need no such guard (but still
   require non-empty stripped content).

5. **Unclosed / empty / malformed expressions stay as text.** If an opening
   delimiter has no matching close in the remaining span, or the stripped content
   is empty, the opening delimiter is literal text — never drop reply content.

6. **Limits carry over from the fence path** and apply across *all* recognised
   expressions (fenced + delimited) combined, in source order:
   - `source` longer than `MAX_LATEX_SOURCE_CHARS` → the expression stays text.
   - After `MAX_LATEX_BLOCKS` expressions have been accepted, later expressions
     stay text.
   These match today's semantics; keep the same constants.

7. **`has_latex_section` is unchanged in signature** and automatically reflects the
   new detection because it delegates to `split_latex_sections`.

### Implementation guidance

- Keep the module pure and side-effect free (no mutation of inputs).
- Prefer a two-layer structure: the existing line walk continues to carve the
  message into fenced-code regions vs. plain text; a new helper scans a plain-text
  span for delimiter expressions and returns an ordered list of sections. Compose
  the two so ordering across the whole message is preserved and adjacent
  `TextSection`s are coalesced (reuse/extend the existing `_append_text` helper).
- Do not use inline imports. Use builtin generics (`list`, `dict`) in annotations,
  no `typing.Optional`/`Union`. Descriptive names, self-documenting code, matching
  the file's existing docstring style.
- Keep `MAX_LATEX_BLOCKS` / `MAX_LATEX_SOURCE_CHARS` as the single source of truth
  (they are also imported by `latex_renderer.py` as its own constant — leave that
  copy alone; do not create a new divergent limit).

## Assumptions

Recorded here because the reviewer was asked but planning did not wait for answers
(thread message-5). Adjust in revision if the reviewer disagrees.

- **A1 — Fence path kept as fallback.** ```` ```latex ```` fences remain recognised
  alongside delimiters, so a model still emitting fences does not regress. (Open
  question Q1.)
- **A2 — Every recognised expression becomes its own image break** in source
  order, including an inline `$x$`/`\(x\)` embedded mid-sentence — which splits that
  sentence into text/image/text messages. This mirrors the current one-image-per-
  expression fence behaviour and reuses the dispatch untouched. Stage 02's prompt
  steers models toward display delimiters for standalone equations. (Open
  question Q2.)
- **A3 — Bare single-`$` is supported** with the currency guard in rule 4, rather
  than restricting to `$$`/`\[`/`\(`. (Open question Q3.)

## Tests (TDD — write these first, in `tests/bot/agents/test_latex_blocks.py`)

Keep every existing test in that file passing (fence behaviour is unchanged). Add:

- Display `$$…$$` split as text / image / text in order; `source` has no `$$`,
  `original` includes them.
- Display `\[…\]` split likewise.
- Inline `\(…\)` split likewise.
- Inline `$…$` with clearly-math content (e.g. `$x^2$`) is extracted.
- Currency guard: `It costs $5 today` and `Between $5 and $10` stay entirely text
  (no `LatexSection`).
- Escaped `\$` stays literal text.
- `$$` wins over `$`: `$$a$$` yields one expression with `source == "a"`, not two
  empty `$`-pairs.
- Delimited expression inside a ```` ```python ```` fence is NOT extracted (fence
  isolation holds); `has_latex_section` is false.
- Delimited expression inside a ```` ```latex ```` fence is handled by the fence path
  (one `LatexSection`), not double-counted.
- Unclosed delimiter (`before $x + y`) stays text.
- Empty delimiter (`$$   $$`, `\(\)`) stays text.
- Oversized delimited source (> `MAX_LATEX_SOURCE_CHARS`) stays text.
- Combined limit: more than `MAX_LATEX_BLOCKS` expressions across mixed
  fence+delimiter forms → only the first `MAX_LATEX_BLOCKS` become `LatexSection`,
  the rest stay text.
- Mixed fence + delimiter in one message keeps both, in source order.
- `has_latex_section` true for a delimiter-only message.

## Definition of done

- `split_latex_sections` / `has_latex_section` recognise the four delimiter forms
  plus existing fences, per the rules above.
- All new and pre-existing tests in `tests/bot/agents/test_latex_blocks.py` pass:
  `uv run pytest tests/bot/agents/test_latex_blocks.py`.
- No changes outside `latex_blocks.py` and its test file. `chat_engine.py`,
  `latex_renderer.py`, and the prompts are untouched; the bot still renders LaTeX
  end to end because the section contract is unchanged.
- Run `semgrep` and `gitleaks` before committing.

## Commit

Conventional commit, e.g.
`feat(chat): break bot replies on standard LaTeX delimiters`.
