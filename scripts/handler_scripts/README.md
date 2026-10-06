# Handler script reference copies

The handler scripts that actually run live in the database — on the
`AdminHandler` / `ChannelHandler` rows the worker loads at fire time
(`smarter_dev/web/admin_handlers_jobs.py`, `smarter_dev/web/handlers_jobs.py`).
Nothing in this directory is loaded by the application; these `.monty` files are
**reference copies**, kept in the repo so a production script can be read,
reviewed and diffed alongside the runtime changes it depends on.

Applying one is a deliberate step, through the bot API — nothing takes a
pasted script: the website's `/admin/handlers` page lists and deletes handlers,
and the Discord `/adminhandler` command hands a description to the author agent,
which writes its own script. `scripts/apply_handler_script.py` does the one
path that exists: it reads the handler back, runs the lint, shows the diff
between the live script and the file, and with `--apply` sends
`PUT /api/admin/handlers/{id}/script` — a route that changes the script and
nothing else (a disabled handler stays disabled, and the script says so) and
refuses if the script moved since it was read — then reads it back:

    BOT_API_KEY=… .venv/bin/python scripts/apply_handler_script.py \
        --guild-id <guild> --handler scam-banner \
        --file scripts/handler_scripts/scam-banner.monty --apply

Run it from a machine with the bot's key, never from inside a production pod.
The judge does not run on this path; a review of the change stands in for it.
Then confirm the next fire's `HandlerRun` outcome is `ok` on
`/admin/handlers?guild_id=<guild>&admin=1`. There is no sync job, and nothing
detects drift — if a handler is edited through `/adminhandler`, update the copy
here in the same change, or it silently becomes stale.

| File | Handler |
|---|---|
| `scam-banner.monty` | `scam-banner`, a guild-wide admin message handler |
