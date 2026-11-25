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
  and to lock a team thread once its last member has left or submissions
  have closed;
- Read Message History in the showcase forum, to find posts it missed and to
  count reactions;
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
Removing a team's only post unlocks the team again. A removed post is listed
under the entries with a "Restore" button, which counts it again. A post is
not restored if its author has since joined or left a team, because they
would be entered twice.

Only threads made by `/jam team` after this feature went live are teams. A
team thread made before that has no team record: its members count as solo
until they run `/jam team` again.

## When submissions close, and results

Every ten minutes the bot asks the web app what each jam needs.

**Missed posts.** While a jam is open, the bot looks through the showcase
forum for posts it never recorded, for example ones made while it was
restarting. It records and tags them as if it had seen them when they were
posted, using the time Discord says the post was made. Only posts made
between the jam's start and its close count.

**Closing.** After submissions close the bot looks for missed posts one last
time. Then it posts a notice in every team thread and locks the thread. The
notice names the team's entry, or says the team did not post one, and says
when voting ends. If a thread cannot be locked the bot tries again on its next
round. The admin page shows "Team threads locked" once every team is done. A
jam that closed more than seven days ago without this happening is left alone.

**Results.** An entry's score is the number of reactions on the first message
of its post when voting ends, exactly as Discord shows them. Every emoji
counts and nothing is left out. Ties go to the entry that more different
people reacted to. Entries level on both share a place and are marked "Tied".

After voting ends the bot counts once and the jam's admin page shows the
Results table: place, entry, reactions, and people who reacted. "Count again"
throws the counts away, and the bot counts again within ten minutes using the
reactions as they are then.

Things to know:

- The bot needs Read Message History in the showcase forum to count. If any
  post cannot be read, nothing is saved and the whole count is tried again.
- The count runs on the bot's first round after voting ends, so reactions
  added in those last minutes are included.
- The count waits for the team threads to be locked. If that keeps failing,
  results appear seven days after submissions closed.
- Super reactions count as reactions, but the people who left them are not in
  "people who reacted".
- A post deleted in Discord before the count is dropped. A post whose first
  message was deleted scores zero.
- Do not move a jam's dates after its submissions have closed. The closing
  notice and the count each run once.

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
