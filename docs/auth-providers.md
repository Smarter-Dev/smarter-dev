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

## Current state: Discord, GitHub and Google (task #71, Oct 2026)

`providers.discord`, `providers.github` and `providers.google` are
configured. Task #56 (Sep 30 2026) switched GitHub and Google off; task #71
switched them back on, because accounts made with them before then could no
longer sign in. Those accounts sign in again through their existing
`skrift.oauth_accounts` rows, matched on provider and provider account id.

Passkey sign-in is still commented out in `app.yaml`, and passkey enrollment
(`second_factors`) is off with it so nobody can add a passkey that cannot sign
in. The account security page still lists passkeys already added, says passkey
sign-in is off and lets the user remove them.

`tests/web/test_sign_in_methods.py` loads the real `app.yaml` into Skrift's
`AuthController` and checks that each of the three providers starts its own
OAuth redirect and has a live callback, that passkey and dummy sign-in 404 on
their direct URLs and that enrollment is refused for a signed-in user. It also
checks what the account security page is given. Update it when providers
change.

### What each provider gives us

Skrift stores the provider's whole profile response
(`oauth_accounts.provider_metadata`), the email and whether the provider
verified it, and the tokens the provider issued on the latest sign-in. Each
sign-in overwrites both tokens, so a sign-in that issues no refresh token
clears the stored one.

- Discord (`identify`, `email`): id, username, global name, avatar, banner,
  accent colour, email, verified flag, locale, MFA flag, Nitro type and
  account flags.
- GitHub (`user:email`): the public profile from `/user` (id, login, name,
  avatar, bio, company, location, blog, follower counts and similar) and the
  primary email from `/user/emails` with its verified flag.
- Google (`openid`, `email`, `profile`, offline access): id, email, verified
  flag, name, given and family name, picture, locale, and the Workspace
  domain (`hd`) for a Workspace account. Skrift asks for offline access, but
  with `prompt=select_account` Google issues a refresh token only on the
  first consent, so the next sign-in clears it.

### While a provider is off

- Users, linked provider accounts (`skrift.oauth_accounts`), passkey
  enrollments and role grants are kept. Nothing is deleted.
- Sessions that already exist stay valid until they expire or the user logs
  out, whichever provider started them.
- A provider's `*_CLIENT_ID`/`*_CLIENT_SECRET` stay in the k8s manifests and
  in `smarter-dev-secrets`; do not delete them, re-enabling needs them.
- A user whose only linked provider is off cannot sign in after their session
  ends. Signing in with another provider follows Skrift's normal account
  resolution:
  - provider account already linked: signs in to that account.
  - provider email matches the account's email and the provider says it is
    verified: the provider is linked to that account and the user is signed
    in.
  - email matches but the provider does not say it is verified: Skrift emails
    a confirmation link to the account's address (valid 15 minutes). Clicking
    it in the same browser links the provider; nothing is linked until then.
  - no account has that email: a new, separate account is created.

## Re-enabling a provider

1. In `app.yaml`, uncomment the provider's block under `auth.providers`
   (delete the leading `# `). For passkeys, uncomment both `methods` and
   `second_factors`; passkey sign-in needs the enrollment store.
2. Check the matching secret keys still exist in `smarter-dev-secrets`
   (`github-client-id`/`-secret`, `google-client-id`/`-secret`) and that the
   provider's OAuth app still lists `https://smarter.dev/auth/<provider>/callback`.
3. Update `tests/web/test_sign_in_methods.py` for the new set of methods.
4. Merge and deploy as usual. `app.yaml` is an image input, so web, bot and
   the workers roll on the new tag; there is no migration.

