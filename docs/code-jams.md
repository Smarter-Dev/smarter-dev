# Code jams

How the jam admin and the bot's `/jam` commands fit together, and what to
watch for when running one.

## Setup

Jams are managed at `/admin/bot/guilds/<guild id>/jams` (sidebar link
"Code Jams"). Creating a jam runs the Discord setup. The button on the jam's
page runs it again: "Finish setup" while something is missing, "Check setup"
after. It is safe to repeat.

Setup makes what is missing:

- a `Code Jams` category;
- `#jam-news`, where members can read and only the bot and server admins post;
- `#jam-chat`, for talk and for team threads;
- a `#jam-showcase` forum, with one tag for the jam and one for its year;
- a `Jammer` role with no permissions, pinged with jam news.

Setup reuses a piece that already exists instead of making a second one:

- a channel is reused when it has the same name and type and sits inside the
  `Code Jams` category. A channel of that name anywhere else is ignored. A
  reused channel keeps its own permissions; setup changes nothing on it;
- a `Jammer` role is reused only when it grants no permissions. If the server
  has a `Jammer` role that grants any, setup reports an error and makes no
  role: rename that role or clear its permissions, then run setup again;
- the showcase can be a forum the server already has. Pick it under "Showcase
  forum" on the Code Jams list page and setup makes no `#jam-showcase`.

The bot needs these permissions:

- Manage Channels and Manage Roles, for setup and for giving the role;
- Create Private Threads, for team threads;
- Manage Threads, to put the jam's tags on members' showcase posts (the tags
  are moderator-only), to remove a team thread that could not be finished,
  and to lock a team thread once its last member has left;
- Mention @everyone, @here and All Roles, to ping the `Jammer` role, which
  members cannot mention themselves.

Without Manage Threads, entries go untagged. Without the mention permission,
jam news posts but pings nobody. Neither shows an error in Discord.

One jam is open at a time, and the next jam reuses the same channels and role.

## Commands

- `/jam subscribe` and `/jam unsubscribe` give and take the Jammer role.
- `/jam team <name>` makes a private team thread under `#jam-chat`.
- `/jam invite <user>`, used inside a team thread, adds a teammate up to the
  jam's team size.
- `/jam leave` takes the member off their team.

## Teams and entries

A member is on one team per jam. `/jam team` and `/jam invite` refuse a member
who is already on a team, until they use `/jam leave`. They also refuse a
member who has already entered on their own. Teams can be made and changed
only while the jam is open.
When the last member leaves, the team is removed and its thread is locked and
archived.

A post in the showcase forum, made while the jam is taking submissions, is an
entry. The bot tags it and records it for the author's team, or for the author
alone if they have no team. Each team, and each solo member, has one entry:
the first post. A later post stays up but does not count, and the bot says
which post does: in the team's thread, or by DM to a solo member.

Once a team has an entry its members are fixed: `/jam invite` and `/jam leave`
stop working for it. The bot says so in the team thread when the entry lands.

Deleting a showcase post in Discord removes its record. If the team has a
later post, that one becomes the entry.

The jam's admin page lists the teams with their members, and every recorded
post marked "Entry" or "Extra post". "Remove entry" stops counting a post and
leaves it in Discord; use it when a team wants a later post to count instead.
Removing a team's only post unlocks the team again.

Only threads made by `/jam team` after this feature went live are teams. A
team thread made before that has no team record: its members count as solo
until they run `/jam team` again.

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

A `#jam-chat` that setup reused keeps the permissions it had. Setup does not
add the thread permission override to it, so add it by hand: deny `@everyone`
Create Private Threads and allow it for the bot. The same goes for a reused
`#jam-news`: check that members cannot post there.
