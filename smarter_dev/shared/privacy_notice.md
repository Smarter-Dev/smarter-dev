Smarter Dev runs a Discord server, a Discord bot and the smarter.dev website. This notice covers what we store about you, why we store it, how long we keep it, who else handles it, and how to have it deleted.

## The short version

- The bot reads messages in the Discord servers it is in, so its features can work: the chat bot, moderation, bytes, squads, quests and challenges.
- The chat bot keeps permanent memories about the people it talks with, written in its own words and naming people by username and Discord ID. It never resets them.
- Moderation history is kept so the server can stay safe, and is not part of a deletion request.
- When your data is deleted, your membership record goes with it; only anonymous usage and cost records stay.
- To have your data deleted, including from the chat bot's memories, DM an @admin on the Smarter Dev Discord server. We delete it within 30 days.

## Who we are

Smarter Dev is run by Smarter Dev LLC. For anything about your data, DM an @admin on the [Smarter Dev Discord server](https://discord.gg/de8kajxbYS) or email [admin@smarter.dev](mailto:admin@smarter.dev).

## What the Discord bot stores

**Messages.** The bot reads messages in the servers it is in. Its AI features need to see what people wrote to answer them, and moderation needs to see what was said to act on it. We do not keep a copy of every message. Almost everywhere the bot keeps a record, the words of your message are replaced with a placeholder before the record is saved. The exceptions are the AI agents' working memory, a few hand-offs between parts of the bot, server automations' own memory and old blog post ideas:

- The chat agent's conversation history expires 2 hours after the conversation goes quiet. Older parts of a long conversation are folded into an AI-written summary.
- The proactive agent, which watches some channels and joins in when it has something useful to say, keeps a running history of what it has read. That history has no time limit. When it grows large, older parts are folded into an AI-written summary.
- The proactive agent also runs as a separate service that reads channel messages from Discord directly and keeps its own copy of its history.
- When a message sets off a server automation, its text is handed to the job that runs the automation and kept for 1 hour.
- Messages waiting for the proactive agent's next look are dropped once they are 48 hours old (checked every 15 minutes), and messages it has picked up to read are kept for up to 48 hours after it picks them up.
- A server automation can be written to carry a message into a reminder it schedules for later. That copy is kept until the reminder runs, and for up to 7 days after.
- A server automation can keep whatever its script chooses between runs, including text copied from a message it reacted to, up to 16 KB (about 16,000 characters). That memory has no time limit: it lasts as long as the automation exists. Automations in the same server can also share a memory of the same size, which stays until an automation deletes it, even after the automations that wrote it are removed. The ready-made automations we provide keep no message text, only things like IDs, counts and times.
- Older versions of the chat agent filed blog post ideas from conversations. They are in its own words, but the evidence listed with them can paraphrase or quote members. The current chat agent no longer files them, though an older version would again if we ran it. Ideas already filed have no time limit.

**The chat bot's memories.** The chat bot keeps a permanent memory for each server: what it knows about the people there, running jokes, opinions it has formed, how it should behave there and who it is there. It writes these in its own words, not as quotes, and it is told not to keep anything private, sensitive or shared in confidence. It names people by username and Discord ID. It also keeps short notes during conversations, which are folded into the memory overnight, and the last five versions of each memory. These memories are permanent and the bot never resets them. Only the bot edits them, including when it removes someone who asked us to delete their data.

**Records of what the AI did.** When an AI feature answers or acts, we keep a record of it: who and which channel it concerned (IDs and usernames), when, which model, how much it cost and what it decided, such as which messages it looked at and whether it replied. The IDs, usernames and numbers stay. The words do not: your messages, and the AI's own replies, running notes, reasoning and what it passed to its tools, are saved as a placeholder instead. Moderation, below, and the old blog post ideas above are the exceptions. When the chat bot searches the web, the search and the results it saw are kept for 48 hours at an unlisted link it can post in Discord. This does not cover the chat bot's memories, described above, which are kept until the bot rewrites them.

**Moderation.** We keep a permanent record of every moderation action taken on a member: who it concerned, who took it, the reason, how long it lasted and when. The bot also posts these actions to the server's moderation log channel, and posts edited and deleted messages, with the old and new text and who wrote them, to the server's audit log channel. Moderation history, including those posts, is kept so the server can stay safe, and is not part of a deletion request.

**Games and community features.** Your bytes balance and the bytes you have sent and received (with the usernames and reasons), your squad membership, your quest and challenge submissions, and the dates of your first and latest message in each server. Leaving a server removes your bytes balance and squad membership there. The rest is kept until you ask us to delete it.

**Commands and automations.** Questions you ask with `/help` (kept for up to 49 hours; the bot's answers are not kept), who asked and when, message counts used for rate limits (kept for a few hours), and automations that server admins set up.

## What the website stores

**Your account.** You sign in with Discord. We receive your Discord ID, username, avatar and email address, and keep them with your account and profile, along with the sign-in tokens Discord gives us. You stay signed in until 30 days after your last visit. If you turn on browser notifications, we keep your browser's notification address. Accounts created before the site moved to Discord-only sign-in may instead hold the name, email address, avatar and sign-in tokens GitHub or Google gave us, and any passkeys added to them; these accounts can no longer sign in, and we delete them on request like any other.

**Chat, search and the AI features.** Your chat conversations, the files you attach, and the AI's answers and summaries are kept until you delete them or your account. Searches you make from your dashboard (the query, the results and the answer) are kept. Search links you share let other people search, and their searches are kept for 30 minutes. Questions you ask about our resources are kept with your account.

**Education products.** We are building education products, Gym and Labs. They are not open yet. Before they collect anything new, we will update this notice to say what they store.

**Billing.** Payments are handled by Polar. We keep a record of your membership while you have an account. When your data is deleted, the membership record is deleted and we keep only anonymous usage and cost records that cannot be linked back to you. Polar keeps its payment records under its own terms.

**Email.** If you sign up for a campaign or waitlist, we keep your email address or Discord ID and send a confirmation email.

**Security.** The API our bot uses records three kinds of security event: failed authentication, requests refused for going over a rate limit, and admin operations. Each records what happened and the kind of request. Rate-limit and admin events also record which API key made the request, and a failed authentication records the IP address it came from. None records a member's Discord ID. Ordinary requests are not logged. These events go to Pydantic Logfire and are kept for 30 days.

## Who else handles your data

- **AI model providers.** Messages, files and questions sent to an AI feature are processed by the model provider serving that request. The chat bot sends each message with its author's Discord user ID, so IDs reach the provider along with the text. The providers are Google, OpenAI, Anthropic, OpenRouter, OpenCode Zen or DigitalOcean. Some messages and searches are also processed by TypeSafe's classifier, which decides things like whether the bot should respond. Web searches go to Brave and web page reads go to Jina. How long they keep it is set by their own terms.
- **Hosting.** Our servers, database and file storage run on DigitalOcean.
- **Payments.** Polar.
- **Email.** Confirmation emails are sent through Resend.
- **Monitoring.** Errors and performance traces from the bot and the website, and security events from the website's API, go to Pydantic Logfire. Errors and traces can include IDs, and some can include the text of a message.
- **Discord.** Everything you post on Discord is also held by Discord under its own privacy policy.

## Deleting your data

DM an @admin on the Smarter Dev Discord server, or email admin@smarter.dev. Either way, we check that you own the Discord account, then an admin deletes your data within 30 days of your request.

**What we delete.** Everything the chat bot holds about you: it removes you from its memories, rewrites its conversation summaries without you and drops the conversation history that holds your messages. Your bytes balance, your squad memberships, quest and challenge submissions, activity dates, forum subscriptions and `/help` records, and your website account with its chat conversations, attachments, searches and profile. Entries about you in server automations' memory, automation jobs about you that are waiting or failed, AI agent sessions that mention you, AI error records and topic notes that name you, and blog post ideas from your conversations or that name you. Records that you share with other people are kept with you removed: bytes transfers you sent or received stay in the other member's history with your ID, username and the reason removed, and records of the bot's conversations and automations that involve other people have your ID and names replaced.

**What we keep.** Moderation history. Anonymous usage and cost records that cannot be linked back to you. A bare record that your request was completed, with nothing that identifies you. Your Discord ID, with nothing else attached, on the chat bot's blocked list. From then on your messages reach the chat bot only as `[BLOCKED BY USER]`, and it does not respond to you.

We also keep a few things we cannot or do not edit for one person:

- If you set up automations, campaigns or scheduled messages as a server admin, your Discord ID or username stays on them as their creator.
- Error and performance logs, in Pydantic Logfire and on our servers, are not edited to remove you.
- Messages the bot posted on Discord, such as replies that name or quote you, are not removed.
- Copies of channel conversations we exported to test the bot's AI are not edited.
- Copies held by the AI providers and other services named above, and by Discord, are governed by their own terms.
- Short-lived records that can still mention you, such as working copies and caches, are gone within 30 days of your request.

If you stay in the server, the bot's other features, such as bytes and activity dates, start new records the next time you post. The blocked list covers the chat bot only: other AI features, such as `/help`, forum replies, server automations and moderation, still process your new messages.

Deleting your website account from your account settings removes the account and its chat conversations and attachments straight away. It does not remove anything the Discord bot stores, the chat bot's memories, or searches you made from your dashboard. For those, DM an @admin or email admin@smarter.dev.

## Changes

We will update this page when what we store changes, and post in the server's privacy channel when we do.
