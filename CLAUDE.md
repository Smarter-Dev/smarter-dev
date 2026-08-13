## Messaging Tools

### `aichat-send` — Send a message to Zech
aichat-send "your message here"

Use this to announce startup, send status updates, and report issues.

### `aichat-unread` — Read unread messages
aichat-unread

Check for new instructions. Run on startup and periodically with `/loop 1m aichat-unread`.

### `aichat-read` — Read recent history
aichat-read [limit]

## Media Service

`services/media` is an internal TypeScript service that owns LaTeX rendering,
audio transcode and Discord card images. It is why `Dockerfile.bot` no longer
installs ffmpeg, nodejs, npm or Pillow's codec headers.

### Two image tags, two scopes

The repo builds three images from two independently computed tags
(`.github/workflows/deploy.yaml`):

| Tag | Images | Computed from |
|---|---|---|
| `IMAGE_TAG` | `smarter-dev-website`, `smarter-dev-bot` | last commit under `.` minus the exclude list (which now includes `services`) |
| `MEDIA_IMAGE_TAG` | `smarter-dev-media` | last commit under `services/media` |

An app-only push leaves `MEDIA_IMAGE_TAG` unchanged, so the media build is
skipped and the media pods never roll. A media-only push does the inverse.

Two invariants keep this honest, and breaking either lets an image change
without its tag changing:

- The `IMAGE_TAG` exclude list must mirror `.dockerignore`. Both now carry
  `services`, and both carry a comment saying they must stay in sync.
- The media build context is `services/media`, not the repo root, so it cannot
  pick up unrelated repo files.

Manifest placeholders are also scoped: every app manifest uses
`<IMAGE_VERSION>`, and `k8s/deploy-media.yaml` uses `<MEDIA_VERSION>`. Do not
add `deploy-media.yaml` to the `<IMAGE_VERSION>` sed loop.

### Wiring

- `MEDIA_SERVICE_URL` (`http://smarter-dev-media:8080`) lives in the
  `smarter-dev-config` ConfigMap; bot and web both pick it up via `envFrom`.
- `MEDIA_API_KEY` comes from `smarter-dev-secrets`, key `media-api-key`, on
  the bot, web and media deployments. The secret key is kebab-case and the
  env var is screaming snake; both spellings are load-bearing.
- The media Service is ClusterIP with no Ingress. The bearer token is defence
  in depth, not the only control.

### Local development

The bot refuses to start without a reachable media service. Locally either:

- `podman compose up media` — builds `services/media` and serves it on host
  port 8083; `.env` needs `MEDIA_SERVICE_URL=http://localhost:8083` and
  `MEDIA_API_KEY=dev-media-key-not-secret` (see `.env.example`). Bot and web
  containers in compose are already wired to `http://media:8080`.
- or `cd services/media && npm install && MEDIA_API_KEY=dev-media-key-not-secret npm run dev`
  (port 8080, needs local `ffmpeg` on PATH) — then point `MEDIA_SERVICE_URL`
  at `http://localhost:8080`.

`services/media/README.md` has the full service-side dev guide.

### Rollout runbook

1. **Before the first deploy**, add `media-api-key` to the live
   `smarter-dev-secrets` — it is not created from `k8s/secrets.template.yaml`.
   `openssl rand -hex 32`, prefixed `mk_`. Without it the media pods fail with
   `CreateContainerConfigError` and the rollout hangs.
2. Deploy. The workflow applies `k8s/deploy-media.yaml` and waits for its
   rollout before applying web and bot. That ordering is load-bearing: bot
   startup health-checks the media service and refuses to come up without it.
3. Rollback is independent per scope. A bad media image rolls back with
   `kubectl set image deployment/smarter-dev-media media=zzmmrmn/smarter-dev-media:<tag>`
   without touching web or bot.
