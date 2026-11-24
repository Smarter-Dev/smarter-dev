# Code jams

How the jam admin and the bot's `/jam` commands fit together, and what to
watch for when running one.

## Setup

Jams are managed at `/admin/bot/guilds/<guild id>/jams` (sidebar link
"Code Jams"). Creating a jam runs the Discord setup. "Finish setup" on the
jam's page runs it again; it is safe to repeat.

Setup makes what is missing and adopts what already exists by name:

- a `Code Jams` category;
- `#jam-news`, where only the bot posts;
- `#jam-chat`, for talk and for team threads;
- a `#jam-showcase` forum, with one tag for the jam and one for its year;
- a `Jammer` role with no permissions, pinged with jam news.

The bot needs Manage Channels, Manage Roles and Create Private Threads. One
jam is open at a time, and the next jam reuses the same channels and role.

## Commands

- `/jam subscribe` and `/jam unsubscribe` give and take the Jammer role.
- `/jam team <name>` makes a private team thread under `#jam-chat`.
- `/jam invite <user>`, used inside a team thread, adds a teammate up to the
  jam's team size.

## Team threads are private: keep handlers off `#jam-chat`

A team is a private thread under `#jam-chat`. Setup denies `@everyone`
Create Private Threads there and allows it for the bot, so a team thread comes
only from `/jam team`.

The bot passes a message posted inside a thread to the handlers set on the
thread's parent channel (`smarter_dev/bot/plugins/handler_events.py`). A
handler or agent set on `#jam-chat` therefore also receives messages from
every private team thread, and its `thread_create` trigger sees team names. A
handler that quotes, summarises or logs what it reads would expose a team's
private work.

Until parent-channel handlers skip private threads by default:

- do not set a handler or agent on `#jam-chat`;
- before each jam, check the channel's handlers in the admin and remove any.

A `#jam-chat` that existed before setup is adopted as it is. Setup does not add
the thread permission override to it, so add it by hand: deny `@everyone`
Create Private Threads and allow it for the bot.
