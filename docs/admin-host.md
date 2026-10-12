# Python app on admin.smarter.dev

The Python web app, bot API and workers remain behind the `smarter-dev-website`
service. During the transition, its ingress accepts both `smarter.dev` and
`admin.smarter.dev`; the public hostname is removed by the separate cutover
task. No DNS or Cloudflare Worker change is part of this deploy.

## Before switching public traffic

1. Point `admin.smarter.dev` at the existing ingress. Verify cert-manager has
   issued `smarter-dev-website-cert` with **both** hostnames in its SANs; the
   ingress TLS entry and rules must contain both hosts until cutover. Confirm
   HTTPS to the admin host reaches the Python app before changing the public
   hostname.
2. Zech adds the following callback URI to each existing provider OAuth app,
   keeping its `smarter.dev` callback until sessions and rollback no longer
   need it:

   | Provider | Callback URI |
   | --- | --- |
   | Discord | `https://admin.smarter.dev/auth/discord/callback` |
   | GitHub | `https://admin.smarter.dev/auth/github/callback` |
   | Google | `https://admin.smarter.dev/auth/google/callback` |

   The separate bot-admin Discord OAuth flow uses
   `https://admin.smarter.dev/bot-admin/auth/discord/callback`; add that URI
   to its Discord app too if the same app is not used for site sign-in.
   `app.yaml` generates the three site callbacks from
   `auth.redirect_base_url`. `DISCORD_REDIRECT_URI` in `k8s/configmap.yaml`
   controls the bot-admin flow.
3. Check a sign-in on `admin.smarter.dev` for each provider. The session cookie
   stays scoped to `.smarter.dev`, so a login is shared with the public site.

## Site service key

After the site memory API lands, create a dedicated Skrift service owner and
grant it the `site-memory` and `site-privacy` permissions used by those API
routes. In `/admin/api-keys`, mint a `sk_` key for that owner with principal
type **service**, service name `cloudflare-site`, and scoped permissions
`site-memory` and `site-privacy`. The owner's permissions and key scopes must
both include those names; Skrift intersects them when authorizing a request.
The key value appears once. Store it in a secret manager immediately.

Patch only the `site-api-key` field of the existing `smarter-dev-secrets`
Kubernetes Secret in namespace `smarter-dev`. Follow the safe one-field
rotation procedure in `docs/v2/legacy-sunset/runbooks/01-rotate-bot-key.md`,
substituting `site-api-key` for `bot-api-key`; avoid replacing the entire
Secret or putting the raw key in a shell argument. The Cloudflare Worker
deployment consumes the same key under its own secret configuration; that
deployment is handled separately. Validate the new key against the memory API
before discarding the one-time value.

## URL ownership

Production `SITE_BASE_URL` points to `admin.smarter.dev` because the Python
app uses it for confirmation emails, account billing, bot configuration,
privacy/terms links and API-generated admin links. The default in
`smarter_dev/shared/config.py` remains `http://localhost:8000` for local
development. The answer view URL follows this setting as well.

Public content literals intentionally stay on `smarter.dev`: canonical and
Open Graph URLs in the resource controllers, the marketing link in the signup
email, and the public URLs in the privacy notice and terms. These identify the
public site after the Worker cutover. Email addresses ending `@smarter.dev`
are contact or sender addresses and are unrelated to routing. Tests using a
`smarter.dev` base explicitly exercise public URLs or URL formatting.
