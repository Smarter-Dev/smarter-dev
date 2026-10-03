Smarter Dev runs a Discord server, a Discord bot and the smarter.dev website. This notice says what we store about you in each of them, why, for how long, who else handles it, and how to ask us to delete it. It describes what the software does today. Where something is not built yet, it says so.

## The short version

- The bot reads messages in the Discord servers it is in, so its features can work: the chat bot, moderation, bytes, squads, quests and challenges.
- The chat bot keeps permanent memories about the people it talks with, written in its own words and naming people by username and Discord ID. It never resets them.
- Moderation history is kept, even after you ask us to delete your data.
- Billing records are kept in anonymised form.
- To have your data deleted, send a direct message to the Smarter Dev admin on our Discord server. There is no opt-out or automatic deletion yet.

## Who we are

Smarter Dev is run by Smarter Dev LLC. For anything about your data, send a direct message to the Smarter Dev admin on the [Smarter Dev Discord server](https://discord.gg/de8kajxbYS).

## What the Discord bot stores

**Messages.** The bot reads messages in the servers it is in. Its AI features need to see what people wrote to answer them, and moderation needs to see what was said to act on it. We do not keep a copy of every message. Almost everywhere the bot keeps a record, the words of your message are replaced with a placeholder before the record is saved. The exceptions are the AI agents' working memory and a few short-lived hand-offs between parts of the bot:

- The chat agent's conversation history expires 2 hours after the conversation goes quiet. Older parts of a long conversation are folded into an AI-written summary.
- The proactive agent, which watches some channels and joins in when it has something useful to say, keeps a running history of what it has read. That history has no time limit. When it grows large, older parts are folded into an AI-written summary.
- Messages waiting to be handed to the proactive agent are normally cleared within 48 hours. A few queues have no time limit. The proactive agent also runs as a separate service that reads channel messages from Discord directly and keeps its own copy of its history.
- Error records that may quote a message are cleared after 48 hours. Background jobs for server automations can hold the message that triggered them for up to 90 days.

**The chat bot's memories.** The chat bot keeps a permanent memory for each server: what it knows about the people there, running jokes, opinions it has formed, how it should behave there and who it is there. It writes these in its own words, not as quotes, and it is told not to keep anything private, sensitive or shared in confidence. It names people by username and Discord ID. It also keeps short notes during conversations, which are folded into the memory overnight, and the last five versions of each memory. These memories are permanent, the bot never resets them, and only the bot edits them.

**Records of what the AI did.** When an AI feature answers or acts, we keep a record of it: who and which channel it concerned (IDs and usernames), when, which model, how much it cost and what it decided. Text the AI wrote, such as its replies and summaries, is cleared from these records after 48 hours. The IDs, usernames and numbers stay.

**Moderation.** We keep a permanent record of every moderation action: who it concerned, who took it, the reason, how long it lasted and when. The bot also posts moderation actions to the server's moderation log channels. Moderation history is kept even after you ask us to delete your data.

**Games and community features.** Your bytes balance and the bytes you have sent and received (with the usernames and reasons), your squad membership, your quest and challenge submissions, and the dates of your first and latest message in each server. Leaving a server removes your bytes balance and squad membership there. The rest is kept until you ask us to delete it.

**Commands and automations.** Questions you ask with `/help` and the bot's answers (the text is cleared after 48 hours; who asked, and when, stays), message counts used for rate limits (kept for a few hours), and automations that server admins set up.

## What the website stores

**Your account.** You sign in with Discord. We receive your Discord ID, username, avatar and email address, and keep them with your account and profile. You stay signed in for up to 30 days.

**Chat, search and the AI features.** Your chat conversations, the files you attach, and the AI's answers and summaries are kept until you delete them or your account. Searches you make from your dashboard (the query, the results and the answer) are kept. Search links you share let other people search, and their searches are kept for 30 minutes. Search previews the bot links to in Discord are deleted after 48 hours. Questions you ask about our resources are kept with your account.

**Education products.** We are building education products, Gym and Labs. They are not open yet. Before they collect anything new, we will update this notice to say what they store.

**Billing.** Payments are handled by Polar. We keep a record of your membership. When your data is deleted, we keep billing records only in anonymised form.

**Email.** If you sign up for a campaign or waitlist, we keep your email address or Discord ID and send a confirmation email.

**Security.** For security and abuse prevention we log your IP address, browser and the pages you request. These logs are deleted after 90 days.

## Who else handles your data

- **AI model providers.** Messages, files and questions sent to an AI feature are processed by the model provider serving that request: Google, OpenAI, Anthropic, OpenRouter, OpenCode Zen or DigitalOcean. Web searches go to Brave and web page reads go to Jina. How long they keep it is set by their own terms.
- **Hosting.** Our servers, database and file storage run on DigitalOcean.
- **Payments.** Polar.
- **Discord.** Everything you post on Discord is also held by Discord under its own privacy policy.

We keep server logs, which can contain IDs and sometimes text, and database backups. We cannot yet delete one person's data from logs or backups.

## Deleting your data

Send a direct message to the Smarter Dev admin on our Discord server. We check that you own the Discord account, then an admin deletes your data by hand.

**What we delete.** Your bytes balance and every bytes transfer you sent or received, your squad memberships, quest and challenge submissions, activity dates, forum subscriptions and `/help` records, and your website account with its chat conversations, attachments, searches and profile. Security log entries under your account are deleted too. Records of what the AI did that other people share, like a chat the bot had with several people, are kept, and we remove your ID and username where they record you as the person who started it.

**What we keep.** Moderation history. Billing records, in anonymised form. A bare record that your request was completed, with nothing that identifies you.

**What we cannot remove yet.** What the chat bot remembers about you, and the summaries in its working history. These are written as shared prose for the whole server and are not yet linked to the people they came from, so we cannot remove one person from them without resetting the bot, which we do not do. We are building a way for the bot to remove everything tied to your ID from its own memories. The same goes for the proactive agent's history and for records that mention your ID inside other people's records. Some copies we cannot reach by hand clear themselves on a timer, from 2 hours for the chat agent's conversation history to 90 days for background job records. The proactive agent's history has no timer. Logs, backups and copies held by AI providers or Discord are not edited.

Deleting your website account from your account settings removes the account and its chat conversations and attachments straight away. It does not remove anything the Discord bot stores, searches you made from your dashboard, or the chat bot's memories. For those, message the admin.

## Changes

We will update this page when what we store changes, and post in the server's privacy channel when we do.
