"""DM ↔ Forum Relay extension.

One persistent forum post per member. ``dm-mirror`` (trigger ``dm_message``)
mirrors an inbound member DM into that member's post — creating the post on the
member's first DM and reusing it thereafter — and sends the one-time monitoring
notice. ``forum-relay`` (trigger ``message``, scoped to the forum channel)
relays every human message typed inside a relay post back to the mapped member
over DM. The member -> post-thread-id map is kept in one guild-shared memory key
(``guild_memory_*``) that both handler rows read/write. See
``docs/v2/feature-parity/staff-communication-channels.md``.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)

MANIFEST = ExtensionManifest(
    slug="dm-forum-relay",
    title="DM ↔ Forum Relay",
    summary=(
        "Gives every member who DMs the bot a persistent staff forum post: "
        "their DMs mirror in, and staff replies typed in the post relay back "
        "to them over DM."
    ),
    version=3,
    config=[
        ConfigField(
            name="forum_channel_id",
            type="channel_id",
            label="Relay forum channel",
            help=(
                "A forum channel. Each member who DMs the bot gets one "
                "persistent post here; type a reply inside a post to DM that "
                "member back."
            ),
        ),
        ConfigField(
            name="notify_on_first_dm",
            type="bool",
            default=True,
            label="Send the one-time monitoring notice",
            help=(
                "DM the member once, the first time they message the bot, to "
                "say their DMs are relayed to staff who may reply."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="dm-mirror",
            name="dm-relay-mirror",
            trigger_type="dm_message",
            description=(
                "Mirrors an inbound member DM into that member's persistent "
                "relay forum post (creating it on their first DM)"
            ),
            script_file="dm_mirror.monty",
            settings={},
            # A dm_message handler is guild-scoped for dispatch (it fires on every
            # DM regardless of channel_ids), but the worker uses channel_ids[0] as
            # the fire's HOME channel — so scoping to the forum channel makes
            # create_post() open the post THERE (create_post has no channel arg;
            # it always targets the home channel).
            channel_scope=["forum_channel_id"],
        ),
        HandlerTemplate(
            key="forum-relay",
            name="dm-relay-forum-reply",
            trigger_type="message",
            description=(
                "Relays any human message typed inside a relay forum post back "
                "to the mapped member over DM"
            ),
            script_file="forum_relay.monty",
            settings={},
            channel_scope=["forum_channel_id"],
        ),
    ],
    example_config={
        "forum_channel_id": "123456789012345678",
        "notify_on_first_dm": True,
    },
)
