# Handler script reference copies

The handler scripts that actually run live in the database — on the
`AdminHandler` / `ChannelHandler` rows the worker loads at fire time
(`smarter_dev/web/admin_handlers_jobs.py`, `smarter_dev/web/handlers_jobs.py`).
Nothing in this directory is loaded by the application; these `.monty` files are
**reference copies**, kept in the repo so a production script can be read,
reviewed and diffed alongside the runtime changes it depends on.

Applying one is manual: paste the file's contents into the handler's script
field in the bot admin UI (the same path an authored/edited handler takes, so
the script still goes through the lint and the judge), then confirm the next
fire's `HandlerRun` outcome is `ok`. There is no sync job, and nothing detects
drift — if you edit a handler in the admin UI, update the copy here in the same
change, or it silently becomes stale.

| File | Handler |
|---|---|
| `scam-banner.monty` | `scam-banner`, a guild-wide admin message handler |
