# `services/media`

Internal-only media service for the smarter-dev cluster. It does three jobs the
bot image used to do in-process:

1. **LaTeX rendering** — MathJax → SVG → sharp → PNG.
2. **Audio transcode** — WAV → Ogg/Opus via `ffmpeg`.
3. **Image cards** — the fourteen 960×540 Discord embed cards.

There is no Ingress and no anonymous access: every route except `/health`
requires `Authorization: Bearer $MEDIA_API_KEY`.

## Local development

```sh
cd services/media
npm install
MEDIA_API_KEY=dev-media-key-not-secret npm run dev
```

`npm run dev` runs `src/main.ts` through `tsx` with watch mode. The process
refuses to start if `MEDIA_API_KEY` is missing or shorter than 16 characters, if
`ffmpeg` is not on `PATH`, or if any font or background asset is missing.

| Script | What it does |
|---|---|
| `npm run dev` | watch-mode server on `MEDIA_PORT` (default 8080) |
| `npm test` | the whole vitest suite; no network, no cluster |
| `npm run typecheck` | `tsc --noEmit` over `src`, `test` and `tools` |
| `npm run build` | emits `dist/` |
| `npm start` | runs the built `dist/main.js` |

### Environment variables

| Name | Default | Meaning |
|---|---|---|
| `MEDIA_API_KEY` | — | **Required.** Shared bearer token, minimum 16 characters. |
| `MEDIA_PORT` | `8080` | Listen port. |
| `MEDIA_LOG_LEVEL` | `info` | pino level. |
| `MEDIA_LATEX_TIMEOUT_MS` | `5000` | Deadline for one `/v1/latex` render. |
| `MEDIA_AUDIO_TIMEOUT_MS` | `30000` | Deadline for one transcode; the ffmpeg child is `SIGKILL`ed. |
| `MEDIA_CARD_TIMEOUT_MS` | `5000` | Deadline for one card render. |
| `MEDIA_IMAGE_TAG` | `dev` | Appended to the version string `/health` reports. |

`ffmpeg` must be installed locally for the audio route. Without it the two audio
integration tests skip and `npm run dev` refuses to start; everything else works.

The suite pins `TZ=UTC` (`vitest.config.ts`), because the `cooldown` fixture
carries a real unix timestamp that the exporter produced with the same pin. Run
`tools/export_media_fixtures.py` under `TZ=UTC` too — it sets that itself.

### Trying it out

```sh
KEY=dev-media-key-not-secret

curl -s localhost:8080/health | jq

curl -s -X POST localhost:8080/v1/latex \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"source":"E = mc^2"}' -o latex.png

curl -s -X POST localhost:8080/v1/cards/balance \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d @test/fixtures/cards/balance.json -o balance.png

curl -s -X POST 'localhost:8080/v1/audio/opus-ogg?bitrate=48k' \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: audio/wav' \
  --data-binary @sample.wav -o voice-message.ogg
```

## Endpoints

```
GET  /health                    (no auth)
POST /v1/latex                  JSON      -> image/png
POST /v1/audio/opus-ogg         audio/wav -> audio/ogg
POST /v1/cards/{simple,error,success,info,cooldown,leaderboard,history,config,
                squad-list,squad-info,squad-members,squad-join-selector,
                balance,transfer-success}
                                JSON      -> image/png
```

Card request bodies use the exact Python parameter names from
`EmbedImageGenerator`. `test/fixtures/cards/*.json` holds one working body per
card. Every non-2xx response is
`{"error": {"code": ..., "message": ..., "detail"?: {...}}}`.

## How the cards stay pixel-faithful

The cards are a port of `smarter_dev/bot/utils/image_embeds.py`, and the port is
checked against ground truth exported from the Pillow implementation.

**Two functions per card.** `src/cards/layout/<card>.ts` is pure: it turns a
request into a `CardLayout`, a flat list of draw ops with absolute coordinates.
`src/cards/paint.ts` rasterises that list. So layout parity and rasterisation
quality are separate test problems.

**Layout parity is exact.** `test/fixtures/layouts/<card>.json` is the draw-op
trace captured from Pillow by monkeypatching `ImageDraw.text` and `.ellipse`.
`test/unit/layout-parity.test.ts` deep-equals each layout against its trace, so
a coordinate can never drift by even one pixel. (Colours compare
case-insensitively: the Python source writes `#00E1FF` next to `#f59e0b` and the
trace preserves both verbatim.)

**Text measurement does not use the canvas.** Skia and FreeType disagree about
glyph advances by fractions of a pixel, and the layouts right-align, centre and
wrap against those numbers. `assets/fonts/pillow-glyph-metrics.json` carries
per-glyph metrics exported straight out of Pillow — advance, ink left, ink
right, ink top, ink bottom, in whole pixels, for every codepoint both fonts
cover plus `.notdef`. `src/cards/metrics.ts` composes string extents from that
table, reproducing `font.getbbox(text)` exactly. `test/unit/metrics.test.ts`
checks it against `test/fixtures/pillow-text-metrics.json`; the export step also
verified the composition against 14,000 random strings with zero mismatches.

**Painting places glyphs one at a time** at the x positions those advances
imply, rather than letting Skia lay out the run, so a long string cannot drift
across its width.

**Golden comparison has three tiers** (`test/golden/cards.golden.test.ts`):
dimensions exactly 960×540; both images box-downsampled to 120×68 with zero
differing cells; and under 3% differing pixels at full resolution. The port
currently sits under 0.25%, all of it glyph edge antialiasing. On failure,
`tools/compare-goldens.ts` writes `test/output/<card>.diff.png`.

**The ported quirks are deliberate.** `squad-members` still draws past y=540 and
gets clipped; `squad-list` still drops squads past index 10 with no truncation
note; `squad-join-selector` still discards the paragraph-break flag; the dead
parameters are still accepted and ignored. Anything worth fixing gets fixed in a
separate commit that regenerates the goldens on both sides at once.

### Two places the port deliberately does not copy Python

- **`squad-info` membership duration** uses the literal calendar fields of
  `member_since`, so an offset-aware timestamp prints a day count instead of
  raising. The Python does `datetime.now() - member_since`, which raises
  `TypeError` on an aware value.
- **Bad TeX** produces MathJax's own error box as a PNG rather than a 422. That
  is what the current worker does too — MathJax 3 renders an `merror` node
  instead of throwing. A 422 `render_failed` is reserved for a renderer
  rejection or an output that busts the 4096 px / 8 MiB caps.

## Regenerating the fixtures

`test/fixtures/{layouts,cards,goldens}/` and `pillow-text-metrics.json`, plus
`assets/fonts/pillow-glyph-metrics.json`, are generated from the Pillow
implementation by `tools/export_media_fixtures.py` — read its docstring, it
needs an archived copy of the pre-rewrite `image_embeds.py`. They are ground truth:
never hand-edit one to make a test pass — that destroys the parity guarantee.
If a fixture looks wrong, fix the exporter and regenerate.

## Layout

```
src/
  main.ts          entrypoint: loadConfig -> deps -> buildServer -> listen
  config.ts        env parsing; throws on a missing or short MEDIA_API_KEY
  server.ts        buildServer(config, deps) -> FastifyInstance (no listen)
  dependencies.ts  the seam tests stub
  auth.ts          bearer-token onRequest hook
  errors.ts        MediaError hierarchy + the pinned error envelope
  withTimeout.ts   per-route deadline
  routes/          health, latex, audio, cards
  latex/           mathjax.ts (init + tex2svg), rasterize.ts (sharp)
  audio/           transcode.ts (ffmpeg)
  cards/           assets, theme, metrics, text, format, paint, registry, layout/
  schemas/         JSON Schema per route body
assets/
  fonts/           BrunoAceSC-Regular.ttf, Anta-Regular.ttf, pillow-glyph-metrics.json
  backgrounds/     background.png, error-background.png, success-background.png
test/
  unit/  http/  golden/  fixtures/
```
