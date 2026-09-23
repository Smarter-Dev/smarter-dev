# Proactive wake label policy v1

This policy labels whether a watcher **must wake the agent**, not whether the
agent would be allowed to respond. The existing `*.labels.json`
`ok_to_respond` value is therefore not ground truth for this benchmark.

Label every human default/reply message with `required_wake`, `category`, and
a short reason in `<fixture>.wake-labels.json`. Do this before inspecting either
candidate's output. Use `null` for genuinely ambiguous cases; those measure
coverage/disagreement but do not enter precision or recall.

Categories:

- `direct_engagement`: the bot is mentioned, replied to, or addressed by name.
- `watch_instruction`: the message satisfies a currently active explicit watch
  instruction.
- `useful_intervention`: an open question/problem where the bot has concrete,
  timely help to add. This is a required wake only when it is not principally a
  conversation directed at specific other people.
- `follow_up`: a promised result or monitored thread has materially advanced.
- `other_user_exchange`: a message directed at specific people other than the
  bot; normally `required_wake: false`.
- `ambient`: social noise, reactions-as-text, greetings, or link drops without
  a concrete bid; normally `required_wake: false`.

A burst's target is `true` if any newly visible message is a required wake,
`false` if every new labeled message is not required, and ambiguous if it has
no required wake and at least one `null` label. Direct Discord mentions and
replies remain deterministic runtime wakes for both candidates.

Adjudication is model-blind. A second reviewer checks every positive and
ambiguous label plus a 20% random sample of negatives. Resolve disagreements
before freezing the manifest; never tune labels after viewing held-out output.

Example sidecar shape (illustrative IDs only):

```json
{
  "policy_version": "proactive-wake-v1",
  "fixture": "guild-channel-2026-09-01.jsonl",
  "labels": {
    "10001": {
      "required_wake": true,
      "category": "direct_engagement",
      "reason": "The message addresses the bot by name with a concrete question."
    },
    "10002": {
      "required_wake": false,
      "category": "other_user_exchange",
      "reason": "This is a reply continuing a conversation between two members."
    }
  }
}
```
