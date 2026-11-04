You maintain the watch instructions a Discord bot set for its watcher (wake
criteria with an id each). A person asked to be removed from the bot's
memory. For EVERY instruction below decide one action:
- `keep` — it has nothing to do with that person; leave it unchanged.
- `rewrite` — it is still useful without them; give the new text in `text`,
  with no trace of the person.
- `drop` — it only makes sense because of them.
Return one decision per instruction id, no more, no fewer.

{target}

INSTRUCTIONS:
{instructions}