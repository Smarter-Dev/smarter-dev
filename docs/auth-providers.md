# Sign-in providers

Production sign-in methods are whatever `auth` in `app.yaml` configures.
Skrift checks that block on every sign-in route: a method that is not
configured gets a 404 on `/auth/<method>/login`, `/auth/<method>/callback`,
`/auth/<method>/options`, `/auth/<method>/complete` and
`/auth/<method>/register/*`, and the login page lists only configured methods.
Hiding a button is never the switch; the config is.

Passkey enrollment (`/auth/passkeys/options` and `/complete`) is a separate
switch, `second_factors`. With it off, a signed-in user with a valid CSRF
token gets 404 `passkey_not_configured`; earlier checks answer 401 without a
session and 400 without CSRF. Removing a kept passkey
(`/account/security/passkeys/<id>/delete`) stays available on purpose.

## Current state: Discord only (task #56, Sep 30 2026)

Only `providers.discord` is configured. GitHub, Google and passkey sign-in are
commented out in `app.yaml`, and passkey enrollment (`second_factors`) is off
with them so nobody can add a passkey that cannot sign in. The account
security page still lists passkeys already added, says passkey sign-in is off
and lets the user remove them, and marks linked providers that cannot sign in
right now.

`tests/web/test_discord_only_login.py` loads the real `app.yaml` into Skrift's
`AuthController` and checks that Discord starts a Discord OAuth redirect, that
every other method 404s on its direct sign-in URLs and that enrollment is
refused for a signed-in user. It also checks what the account security page is
given. Update it when providers change.

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
  resolution, which was not changed:
  - Discord account already linked: signs in to that account.
  - Discord email matches the account's email and Discord says it is
    verified: Discord is linked to that account and the user is signed in.
  - Email matches but Discord does not say it is verified: Skrift emails a
    confirmation link to the account's address (valid 15 minutes). Clicking it
    in the same browser links Discord; nothing is linked until then.
  - No account has that email: a new, separate account is created.
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
