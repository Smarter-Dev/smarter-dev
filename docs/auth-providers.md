# Sign-in providers

Production sign-in methods are whatever `auth` in `app.yaml` configures.
Skrift reads that block for every auth route: a method that is not configured
gets a 404 on `/auth/<method>/login`, `/auth/<method>/callback` and the
passkey `options`/`complete`/`register` endpoints, and the login page lists
only configured methods. Hiding a button is never the switch; the config is.

## Current state: Discord only (task #56, Sep 30 2026)

Only `providers.discord` is configured. GitHub, Google and passkey sign-in are
commented out in `app.yaml`, and passkey enrollment (`second_factors`) is off
with them so nobody can add a passkey that cannot sign in.

`tests/web/test_discord_only_login.py` loads the real `app.yaml` into Skrift's
`AuthController` and checks that Discord starts a Discord OAuth redirect while
every other method 404s on direct URLs. Update it when providers change.

### What this does and does not touch

- Users, linked provider accounts (`skrift.oauth_accounts`), passkey
  enrollments and role grants are kept. Nothing is deleted.
- Sessions that already exist stay valid until they expire or the user logs
  out, whichever provider started them.
- `GITHUB_CLIENT_*` and `GOOGLE_CLIENT_*` stay in `k8s/deploy.yaml` and in
  `smarter-dev-secrets`. They are unused while the providers are off; do not
  delete them, re-enabling needs them.
- A user whose only linked provider is GitHub or Google cannot sign in after
  their session ends. Signing in with Discord follows Skrift's normal account
  resolution: if Discord reports a verified email that matches the account's
  email, Discord is linked to that account; otherwise a new account is
  created. Linking policy was not changed.
- A GitHub or Google sign-in started before the deploy that is waiting on an
  email-link confirmation (`/auth/verify-email/claim/...`) can still complete
  for up to 15 minutes (Skrift's `EMAIL_LINK_TTL_SECONDS`). After that no path
  remains.

## Re-enabling a provider

1. In `app.yaml`, uncomment the provider's block under `auth.providers`
   (delete the leading `# `). For passkeys, uncomment both `methods` and
   `second_factors`; passkey sign-in needs the enrollment store.
2. Check the matching secret keys still exist in `smarter-dev-secrets`
   (`github-client-id`/`-secret`, `google-client-id`/`-secret`) and that the
   provider's OAuth app still lists `https://smarter.dev/auth/<provider>/callback`.
3. Update `tests/web/test_discord_only_login.py` for the new set of methods.
4. Merge and deploy as usual. `app.yaml` is an image input, so web, bot and
   the workers roll on the new tag; there is no migration.

Reverting the task #56 commit does steps 1 and 3 in one go.
