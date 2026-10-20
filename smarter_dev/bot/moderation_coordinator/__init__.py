"""The moderation coordinator: one decision per message revision.

Today four detectors (the spam engine, the attachment filter, the spam ring
and the scam-banner handler, plus edit-ping-catch and the Disboard channel
rule) each judge a message and act on it through the moderation gateway. The
coordinator judges a message once:

1. **Facts** (:mod:`.model`): one snapshot of the message revision, its author
   and the guild policy.
2. **Rules** (:mod:`.rules`): pure functions. Each returns what fired, who
   it would act on, the sanction it proposes, which messages it would clean up,
   and which classifier labels it needs before it can decide. No rule acts,
   reports or calls a model.
3. **One classifier request** (:mod:`.classifier`) for the union of the labels
   every rule asked for, on one backend: Claude Haiku 5.5 when a label needs
   it (scam, abuse), otherwise Jev. No tools, no retries, a deadline and a
   budget; anything it cannot answer is *unknown*.
4. **One decision** (:mod:`.decide`): the strongest sanction per target, the
   deduplicated cleanup, and the provisional timeout a high-signal scam gets
   while it is judged.

In this step it runs in **shadow** (:mod:`.shadow`): it records what it would
have done next to what the legacy actors did, and it cannot touch Discord.
The cutover (step 4) hands its decision to the gateway instead.
"""
