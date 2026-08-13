"""Export the Pillow ground truth the TypeScript media service is tested against.

Everything this writes is generated output, not implementation. Never hand-edit
one of these files to make a test pass; fix the exporter and regenerate.

The Pillow implementation this reads is being deleted by the Python-side
rewrite, so keep a copy of the last version that still had the PIL drawing code
next to this script:

    git show <commit>:smarter_dev/bot/utils/image_embeds.py \
      > services/media/tools/legacy_image_embeds.py

Then, from the repo root:

    uv run --with fonttools python services/media/tools/export_media_fixtures.py

Emits, under services/media/:

    assets/fonts/pillow-glyph-metrics.json     runtime asset: per-glyph metrics
    test/fixtures/pillow-text-metrics.json     ground-truth string measurements
    test/fixtures/layouts/<card>.json          draw-op traces
    test/fixtures/cards/<card>.json            HTTP request bodies
    test/fixtures/goldens/<card>.png           golden PNGs at FIXED_NOW
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile
import time as time_module

# Two cards read a clock, and one of them (`cooldown`) compares a real unix
# timestamp. Pinning the exporter and the vitest suite to the same zone is what
# makes those fixtures reproducible on any machine.
os.environ["TZ"] = "UTC"
time_module.tzset()
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import ImageDraw

SERVICE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_ROOT.parent.parent
sys.path.insert(0, str(SERVICE_ROOT / "tools"))


def load_generator_class():
    """The PIL `EmbedImageGenerator`, from the repo or from the archived copy."""
    try:
        from legacy_image_embeds import EmbedImageGenerator
    except ImportError:
        from smarter_dev.bot.utils.image_embeds import EmbedImageGenerator
    if "resources_path" not in inspect.signature(EmbedImageGenerator.__init__).parameters:
        raise RuntimeError(
            "EmbedImageGenerator no longer draws with Pillow. Archive the last "
            "PIL version as services/media/tools/legacy_image_embeds.py first."
        )
    return EmbedImageGenerator


EmbedImageGenerator = load_generator_class()

FIXTURES = SERVICE_ROOT / "test" / "fixtures"

FIXED_NOW = datetime(2026, 8, 6, 12, 0, 0)
REFERENCE_NOW = FIXED_NOW

FONT_FILES = {
    "title_large": ("BrunoAceSC-Regular.ttf", 60),
    "title_medium": ("BrunoAceSC-Regular.ttf", 48),
    "title_small": ("BrunoAceSC-Regular.ttf", 36),
    "text_large": ("Anta-Regular.ttf", 32),
    "text_medium": ("Anta-Regular.ttf", 28),
    "text_small": ("Anta-Regular.ttf", 24),
    "text_tiny": ("Anta-Regular.ttf", 20),
}

# Everything is read from the service's own assets, so the exporter keeps working
# after the repo-level `resources/discord-embeds/` tree is deleted.
ASSETS = SERVICE_ROOT / "assets"

FONT_PATHS = {
    "BrunoAceSC-Regular.ttf": ASSETS / "fonts/BrunoAceSC-Regular.ttf",
    "Anta-Regular.ttf": ASSETS / "fonts/Anta-Regular.ttf",
}


def build_resources_shim(root: Path) -> Path:
    """The `resources/` tree layout `EmbedImageGenerator` expects, from `assets/`."""
    (root / "discord-embeds").mkdir(parents=True, exist_ok=True)
    for name in ("background.png", "error-background.png", "success-background.png"):
        shutil.copyfile(ASSETS / "backgrounds" / name, root / "discord-embeds" / name)

    (root / "fonts" / "Bruno_Ace_SC").mkdir(parents=True, exist_ok=True)
    (root / "fonts" / "Anta").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        ASSETS / "fonts/BrunoAceSC-Regular.ttf",
        root / "fonts/Bruno_Ace_SC/BrunoAceSC-Regular.ttf",
    )
    shutil.copyfile(ASSETS / "fonts/Anta-Regular.ttf", root / "fonts/Anta/Anta-Regular.ttf")
    return root


# --------------------------------------------------------------------------- #
# Scenario data (mirrors scratchpad/media-service/render_goldens.py)
# --------------------------------------------------------------------------- #


@dataclass
class LeaderboardEntry:
    rank: int
    user_id: str
    balance: int
    streak_count: int


@dataclass
class Transaction:
    created_at: datetime
    giver_id: str
    giver_username: str
    receiver_id: str
    receiver_username: str
    amount: int
    reason: str | None = None


@dataclass
class BytesConfig:
    daily_amount: int
    starting_balance: int
    max_transfer: int
    transfer_cooldown_hours: int
    streak_bonuses: dict[str, int]


@dataclass
class Squad:
    id: str
    name: str
    description: str | None
    role_id: str
    switch_cost: int
    current_join_cost: int
    has_join_sale: bool
    member_count: int
    max_members: int | None
    is_active: bool
    is_default: bool = False


@dataclass
class SquadMember:
    user_id: str
    username: str
    joined_at: datetime


@dataclass
class UserMemberInfo:
    member_since: datetime


LEADERBOARD_ENTRIES = [
    LeaderboardEntry(1, "234981726354091520", 18420, 27),
    LeaderboardEntry(2, "112233445566778899", 15075, 12),
    LeaderboardEntry(3, "998877665544332211", 11340, 0),
    LeaderboardEntry(4, "445566778899001122", 8925, 5),
    LeaderboardEntry(5, "556677889900112233", 7710, 41),
    LeaderboardEntry(6, "667788990011223344", 5480, 0),
    LeaderboardEntry(7, "778899001122334455", 3265, 3),
    LeaderboardEntry(8, "889900112233445566", 1990, 1),
    LeaderboardEntry(9, "990011223344556677", 1245, 0),
    LeaderboardEntry(10, "001122334455667788", 640, 8),
]

LEADERBOARD_DISPLAY_NAMES = {
    "234981726354091520": "nyxbyte",
    "112233445566778899": "Quinn Alvarez",
    "998877665544332211": "der_kompilator_9000",
    "445566778899001122": "mochi",
    "556677889900112233": "Sam Whitfield",
    "667788990011223344": "packet_gremlin",
    "778899001122334455": "Ada L.",
    "889900112233445566": "zerocool",
    "001122334455667788": "rustacean_rae",
}

SELF_USER_ID = "234981726354091520"

TRANSACTIONS = [
    Transaction(REFERENCE_NOW - timedelta(hours=3), "SYSTEM", "System", SELF_USER_ID, "nyxbyte", 80, "Daily reward (Day 27, 4x multiplier)"),
    Transaction(REFERENCE_NOW - timedelta(days=1, hours=5), SELF_USER_ID, "nyxbyte", "112233445566778899", "Quinn Alvarez", 250, "thanks for the code review"),
    Transaction(REFERENCE_NOW - timedelta(days=2, hours=1), SELF_USER_ID, "nyxbyte", "SYSTEM", "System", 500, "Squad join fee: Nightshade Collective"),
    Transaction(REFERENCE_NOW - timedelta(days=4), "998877665544332211", "der_kompilator_9000", SELF_USER_ID, "nyxbyte", 1200, "bounty payout"),
    Transaction(REFERENCE_NOW - timedelta(days=6), "SYSTEM", "System", SELF_USER_ID, "nyxbyte", 100, "New member welcome bonus"),
    Transaction(REFERENCE_NOW - timedelta(days=8), SELF_USER_ID, "nyxbyte", "445566778899001122", "mochi", 75, None),
    Transaction(REFERENCE_NOW - timedelta(days=11), "SYSTEM", "System", SELF_USER_ID, "nyxbyte", 20, "Daily reward (Day 3)"),
    Transaction(REFERENCE_NOW - timedelta(days=14), "556677889900112233", "Sam Whitfield", SELF_USER_ID, "nyxbyte", 340, "split the jam prize"),
]

BYTES_CONFIG = BytesConfig(25, 100, 5000, 6, {"7": 2, "14": 4, "30": 8, "60": 16})

SQUADS = [
    Squad(
        "7f0a2c1e-1111-4a00-9c11-000000000001",
        "Nightshade Collective",
        "Systems folks who live in the terminal. We take on the gnarly infrastructure bounties and hand out post-mortems like candy.",
        "401122334455667701", 500, 350, True, 18, 25, True,
    ),
    Squad("7f0a2c1e-1111-4a00-9c11-000000000002", "Copper Wren", "Front-end and design crew.", "401122334455667702", 500, 500, False, 12, 25, True),
    Squad("7f0a2c1e-1111-4a00-9c11-000000000003", "The Long Tail Analytics Guild", "Data wranglers.", "401122334455667703", 750, 750, False, 9, None, True),
    Squad("7f0a2c1e-1111-4a00-9c11-000000000004", "Drifters", "Everyone starts here.", "401122334455667704", 0, 0, False, 204, None, True, True),
    Squad("7f0a2c1e-1111-4a00-9c11-000000000005", "Firebreak", "Incident response volunteers.", "401122334455667705", 0, 0, False, 6, 10, True),
]

GUILD_ROLES = {
    "401122334455667701": 0x9B59B6,
    "401122334455667702": 0xE67E22,
    "401122334455667703": 0x3498DB,
    "401122334455667704": 0x000000,
    "401122334455667705": 0xE74C3C,
}

SQUAD_MEMBERS = [
    SquadMember("234981726354091520", "nyxbyte", REFERENCE_NOW - timedelta(days=120)),
    SquadMember("112233445566778899", "Quinn Alvarez", REFERENCE_NOW - timedelta(days=98)),
    SquadMember("998877665544332211", "der_kompilator_9000", REFERENCE_NOW - timedelta(days=77)),
    SquadMember("445566778899001122", "mochi", REFERENCE_NOW - timedelta(days=64)),
    SquadMember("556677889900112233", "Sam Whitfield", REFERENCE_NOW - timedelta(days=51)),
    SquadMember("667788990011223344", "packet_gremlin", REFERENCE_NOW - timedelta(days=44)),
    SquadMember("778899001122334455", "Ada L.", REFERENCE_NOW - timedelta(days=38)),
    SquadMember("889900112233445566", "zerocool", REFERENCE_NOW - timedelta(days=30)),
    SquadMember("990011223344556677", "quietstorm", REFERENCE_NOW - timedelta(days=25)),
    SquadMember("001122334455667788", "rustacean_rae", REFERENCE_NOW - timedelta(days=19)),
    SquadMember("110022334455667799", "bitrot_barnaby_the_third", REFERENCE_NOW - timedelta(days=14)),
    SquadMember("220033445566778800", "Priya N.", REFERENCE_NOW - timedelta(days=11)),
    SquadMember("330044556677889911", "onyx", REFERENCE_NOW - timedelta(days=8)),
    SquadMember("440055667788990022", "lambda_lou", REFERENCE_NOW - timedelta(days=5)),
    SquadMember("550066778899001133", "Theo Brandt", REFERENCE_NOW - timedelta(days=3)),
    SquadMember("660077889900112244", "newcomer_nina", REFERENCE_NOW - timedelta(days=1)),
    SquadMember("770088990011223355", "gray_goose", REFERENCE_NOW - timedelta(hours=9)),
]

COOLDOWN_END = int((REFERENCE_NOW + timedelta(hours=6)).timestamp())


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def leaderboard_entry_body(entry: LeaderboardEntry) -> dict:
    return {"rank": entry.rank, "user_id": entry.user_id, "balance": entry.balance, "streak_count": entry.streak_count}


def transaction_body(transaction: Transaction) -> dict:
    return {
        "created_at": iso(transaction.created_at),
        "giver_id": transaction.giver_id,
        "giver_username": transaction.giver_username,
        "receiver_id": transaction.receiver_id,
        "receiver_username": transaction.receiver_username,
        "amount": transaction.amount,
        "reason": transaction.reason,
    }


def squad_body(squad: Squad) -> dict:
    return {
        "name": squad.name,
        "description": squad.description,
        "member_count": squad.member_count,
        "max_members": squad.max_members,
        "switch_cost": squad.switch_cost,
        "current_join_cost": squad.current_join_cost,
        "has_join_sale": squad.has_join_sale,
        "role_id": squad.role_id,
        "is_default": squad.is_default,
        "is_active": squad.is_active,
    }


def member_body(member: SquadMember) -> dict:
    return {"user_id": member.user_id, "username": member.username, "joined_at": iso(member.joined_at)}


def scenarios(generator: EmbedImageGenerator) -> list[tuple[str, dict, object]]:
    """(card name, HTTP request body, zero-arg callable that renders it)."""
    return [
        (
            "simple",
            {
                "title": "WELCOME TO SMARTER DEV",
                "description": (
                    "You earn bytes for showing up and helping out.\n\n"
                    "Use /bytes balance to see your total, /bytes send to pass some "
                    "along, and /squads list to find a crew to run with."
                ),
                "embed_type": "default",
            },
            lambda: generator.create_simple_embed(
                "WELCOME TO SMARTER DEV",
                "You earn bytes for showing up and helping out.\n\n"
                "Use /bytes balance to see your total, /bytes send to pass some "
                "along, and /squads list to find a crew to run with.",
                "default",
            ),
        ),
        (
            "error",
            {
                "message": (
                    "You do not have enough bytes for that transfer. "
                    "Your balance is 640 bytes and you tried to send 1,200."
                )
            },
            lambda: generator.create_error_embed(
                "You do not have enough bytes for that transfer. "
                "Your balance is 640 bytes and you tried to send 1,200."
            ),
        ),
        (
            "success",
            {
                "title": "DAILY CLAIMED",
                "description": "You picked up 80 bytes today (Day 27 streak, 4x multiplier).\nNew balance: 18,420 bytes.",
            },
            lambda: generator.create_success_embed(
                "DAILY CLAIMED",
                "You picked up 80 bytes today (Day 27 streak, 4x multiplier).\nNew balance: 18,420 bytes.",
            ),
        ),
        (
            "info",
            {
                "title": "SQUAD SWITCHING LOCKED",
                "description": "A campaign is running in this server, so squad membership is frozen until it wraps up on August 14th.",
            },
            lambda: generator.create_info_embed(
                "SQUAD SWITCHING LOCKED",
                "A campaign is running in this server, so squad membership is frozen until it wraps up on August 14th.",
            ),
        ),
        (
            "cooldown",
            {"message": "You are on transfer cooldown.", "cooldown_end_timestamp": COOLDOWN_END},
            lambda: generator.create_cooldown_embed("You are on transfer cooldown.", COOLDOWN_END),
        ),
        (
            "leaderboard",
            {
                "entries": [leaderboard_entry_body(e) for e in LEADERBOARD_ENTRIES],
                "guild_name": "Smarter Dev",
                "user_display_names": LEADERBOARD_DISPLAY_NAMES,
            },
            lambda: generator.create_leaderboard_embed(LEADERBOARD_ENTRIES, "Smarter Dev", LEADERBOARD_DISPLAY_NAMES),
        ),
        (
            "history",
            {"transactions": [transaction_body(t) for t in TRANSACTIONS], "user_id": SELF_USER_ID},
            lambda: generator.create_history_embed(TRANSACTIONS, SELF_USER_ID),
        ),
        (
            "config",
            {
                "config": {
                    "daily_amount": BYTES_CONFIG.daily_amount,
                    "starting_balance": BYTES_CONFIG.starting_balance,
                    "max_transfer": BYTES_CONFIG.max_transfer,
                    "transfer_cooldown_hours": BYTES_CONFIG.transfer_cooldown_hours,
                    "streak_bonuses": BYTES_CONFIG.streak_bonuses,
                },
                "guild_name": "Smarter Dev",
            },
            lambda: generator.create_config_embed(BYTES_CONFIG, "Smarter Dev"),
        ),
        (
            "squad-list",
            {
                "squads": [squad_body(s) for s in SQUADS],
                "guild_name": "Smarter Dev",
                "current_squad_id": SQUADS[0].id,
                "guild_roles": GUILD_ROLES,
                "has_active_campaign": True,
            },
            lambda: generator.create_squad_list_embed(
                SQUADS, "Smarter Dev", current_squad_id=SQUADS[0].id, guild_roles=GUILD_ROLES, has_active_campaign=True
            ),
        ),
        (
            "squad-info",
            {
                "squad": squad_body(SQUADS[0]),
                "members": [member_body(m) for m in SQUAD_MEMBERS],
                "user_member_info": {"member_since": iso(REFERENCE_NOW - timedelta(days=120))},
            },
            lambda: generator.create_squad_info_embed(
                SQUADS[0], SQUAD_MEMBERS, UserMemberInfo(member_since=REFERENCE_NOW - timedelta(days=120))
            ),
        ),
        (
            "squad-members",
            {"squad": squad_body(SQUADS[0]), "members": [member_body(m) for m in SQUAD_MEMBERS]},
            lambda: generator.create_squad_members_embed(SQUADS[0], SQUAD_MEMBERS),
        ),
        (
            "squad-join-selector",
            {"user_balance": 18420, "current_squad_name": "Nightshade Collective", "available_squads_count": len(SQUADS)},
            lambda: generator.create_squad_join_selector_embed(
                18420, current_squad_name="Nightshade Collective", available_squads_count=len(SQUADS)
            ),
        ),
        (
            "balance",
            {
                "username": "nyxbyte",
                "balance": 18420,
                "streak_count": 27,
                "last_daily": "2026-08-06",
                "total_received": 24680,
                "total_sent": 6260,
            },
            lambda: generator.create_balance_embed(
                "nyxbyte", 18420, streak_count=27, last_daily="2026-08-06", total_received=24680, total_sent=6260
            ),
        ),
        (
            "transfer-success",
            {
                "giver_name": "nyxbyte",
                "receiver_name": "Quinn Alvarez",
                "amount": 250,
                "reason": "thanks for the code review",
                "new_balance": 18170,
            },
            lambda: generator.create_transfer_success_embed(
                "nyxbyte", "Quinn Alvarez", 250, reason="thanks for the code review", new_balance=18170
            ),
        ),
    ]


# --------------------------------------------------------------------------- #
# Draw-op tracing
# --------------------------------------------------------------------------- #


class Tracer:
    """Records every ImageDraw call the generator makes for one card."""

    def __init__(self, font_keys: dict[int, str]) -> None:
        self._font_keys = font_keys
        self.ops: list[dict] = []
        self.background = "background.png"

    def record_text(self, xy, text, font, fill) -> None:
        x, y = xy
        self.ops.append(
            {
                "op": "text",
                "x": int(x),
                "y": int(y),
                "text": text,
                "font": self._font_keys[id(font)],
                "fill": fill,
            }
        )

    def record_ellipse(self, xy, fill) -> None:
        x0, y0, x1, y1 = xy
        self.ops.append(
            {
                "op": "ellipse",
                "cx": int((x0 + x1) / 2),
                "cy": int((y0 + y1) / 2),
                "r": int((x1 - x0) / 2),
                "fill": fill,
            }
        )


def trace_card(generator: EmbedImageGenerator, card: str, render) -> tuple[dict, bytes]:
    font_keys = {id(font): key for key, font in generator._fonts.items()}
    tracer = Tracer(font_keys)

    original_text = ImageDraw.ImageDraw.text
    original_ellipse = ImageDraw.ImageDraw.ellipse
    original_background = EmbedImageGenerator._get_background

    def traced_text(self, xy, text, fill=None, font=None, *args, **kwargs):
        tracer.record_text(xy, text, font, fill)
        return original_text(self, xy, text, fill=fill, font=font, *args, **kwargs)

    def traced_ellipse(self, xy, fill=None, outline=None, width=1):
        tracer.record_ellipse(xy, fill)
        return original_ellipse(self, xy, fill=fill, outline=outline, width=width)

    def traced_background(self, embed_type="default"):
        files = {
            "error": "error-background.png",
            "success": "success-background.png",
            "default": "background.png",
            "warning": "background.png",
            "info": "background.png",
        }
        tracer.background = files.get(embed_type, "background.png")
        return original_background(self, embed_type)

    ImageDraw.ImageDraw.text = traced_text
    ImageDraw.ImageDraw.ellipse = traced_ellipse
    EmbedImageGenerator._get_background = traced_background
    try:
        rendered = render()
    finally:
        ImageDraw.ImageDraw.text = original_text
        ImageDraw.ImageDraw.ellipse = original_ellipse
        EmbedImageGenerator._get_background = original_background

    layout = {
        "card": card,
        "background": tracer.background,
        "canvas": {"width": 960, "height": 540},
        "ops": tracer.ops,
    }
    return layout, rendered.data


# --------------------------------------------------------------------------- #
# Font metrics
# --------------------------------------------------------------------------- #

NOTDEF_PROBE = ""  # private-use, absent from both fonts


def glyph_entry(font, character: str) -> list[int]:
    """[advance, inkLeft, inkRight, inkTop, inkBottom] for one glyph.

    Every value is Pillow's own, so composing them reproduces `getbbox` exactly.
    `inkLeft` is `min(0, bearingX)` and `inkRight` is `max(advance, bearingX +
    bitmapWidth)`, which is precisely what single-character `getbbox` reports.
    A blank glyph has inkTop == inkBottom.
    """
    bbox = font.getbbox(character)
    advance = font.getlength(character)
    if advance != int(advance):
        raise ValueError(f"non-integral advance {advance} for {character!r}")
    return [int(advance), bbox[0], bbox[2], bbox[1], bbox[3]]


def export_glyph_metrics(generator: EmbedImageGenerator) -> dict:
    """Per-codepoint advance and ink extents, exactly as Pillow measures them."""
    covered: dict[str, list[int]] = {}
    for file_name, path in FONT_PATHS.items():
        cmap = TTFont(str(path)).getBestCmap()
        covered[file_name] = sorted(cp for cp in cmap if cp >= 0x20)

    fonts = {}
    for key, (file_name, px) in FONT_FILES.items():
        font = generator._fonts[key]
        ascent, descent = font.getmetrics()
        glyphs = {str(cp): glyph_entry(font, chr(cp)) for cp in covered[file_name]}
        fonts[key] = {
            "file": file_name,
            "px": px,
            "ascent": ascent,
            "descent": descent,
            "notdef": glyph_entry(font, NOTDEF_PROBE),
            "glyphs": glyphs,
        }
    return {"fonts": fonts}


def compose(entry: dict, text: str) -> tuple[int, int, int, int]:
    """Reconstruct Pillow's getbbox from the per-glyph table."""
    if not text:
        return (0, 0, 0, 0)
    ascent = entry["ascent"]
    pen = 0
    left = 0
    right = 0
    top = None
    bottom = None
    for character in text:
        glyph = entry["glyphs"].get(str(ord(character)), entry["notdef"])
        left = min(left, pen + glyph[1])
        right = max(right, pen + glyph[2])
        pen += glyph[0]
        if glyph[3] != glyph[4]:
            top = glyph[3] if top is None else min(top, glyph[3])
            bottom = glyph[4] if bottom is None else max(bottom, glyph[4])
    if top is None:
        top = bottom = ascent
    return (left, top, max(pen, right), bottom)


CORPUS = [
    "",
    " ",
    "A",
    "AV",
    "Wg",
    "Balance:",
    "1,234,567",
    "  7 days: 2x • 14 days: 4x • 30 days: 8x • 60 days: 16x",
    "café naïve Ünter",
    "日本語テキスト",
    "emoji \U0001f600 here",
    "!\"#$%&'()*+,-./0123456789:;<=>?@",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`",
    "abcdefghijklmnopqrstuvwxyz{|}~",
    "... and 2 more members",
    "Campaign active - Switching disabled",
]


def export_text_metrics(generator: EmbedImageGenerator, drawn_strings: dict[str, set[str]]) -> dict:
    fonts = {}
    for key, (file_name, px) in FONT_FILES.items():
        ascent, descent = generator._fonts[key].getmetrics()
        fonts[key] = {"file": file_name, "px": px, "ascent": ascent, "descent": descent}

    measurements = []
    for key in FONT_FILES:
        font = generator._fonts[key]
        strings = sorted(drawn_strings.get(key, set()) | set(CORPUS))
        for text in strings:
            bbox = font.getbbox(text)
            measurements.append(
                {
                    "font": key,
                    "text": text,
                    "bbox": list(bbox),
                    "advance": int(font.getlength(text)),
                }
            )
    return {"fonts": fonts, "measurements": measurements}


# --------------------------------------------------------------------------- #


def main() -> None:
    real_time = time_module.time

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_NOW

    time_module.time = lambda: FIXED_NOW.timestamp()
    import datetime as datetime_module

    original_datetime_class = datetime_module.datetime
    datetime_module.datetime = FrozenDatetime

    try:
        shim = build_resources_shim(Path(tempfile.mkdtemp(prefix="media-resources-")))
        generator = EmbedImageGenerator(shim)

        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / "layouts").mkdir(exist_ok=True)
        (FIXTURES / "cards").mkdir(exist_ok=True)
        (FIXTURES / "goldens").mkdir(exist_ok=True)

        drawn_strings: dict[str, set[str]] = {}

        for card, body, render in scenarios(generator):
            layout, png = trace_card(generator, card, render)
            (FIXTURES / "layouts" / f"{card}.json").write_text(json.dumps(layout, indent=2, ensure_ascii=False) + "\n")
            (FIXTURES / "cards" / f"{card}.json").write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
            (FIXTURES / "goldens" / f"{card}.png").write_bytes(png)
            for op in layout["ops"]:
                if op["op"] == "text":
                    drawn_strings.setdefault(op["font"], set()).add(op["text"])
            print(f"{card:<22} {len(layout['ops']):>4} ops  {len(png):>8} bytes")

        glyph_metrics = export_glyph_metrics(generator)
        asset_path = SERVICE_ROOT / "assets" / "fonts" / "pillow-glyph-metrics.json"
        asset_path.write_text(json.dumps(glyph_metrics, separators=(",", ":")) + "\n")
        print(f"\nglyph metrics -> {asset_path} ({asset_path.stat().st_size} bytes)")

        text_metrics = export_text_metrics(generator, drawn_strings)
        (FIXTURES / "pillow-text-metrics.json").write_text(json.dumps(text_metrics, indent=2, ensure_ascii=False) + "\n")
        print(f"text metrics  -> {len(text_metrics['measurements'])} measurements")

        # Verify the per-glyph table reproduces Pillow exactly for every measured string.
        mismatches = 0
        for measurement in text_metrics["measurements"]:
            entry = glyph_metrics["fonts"][measurement["font"]]
            composed = list(compose(entry, measurement["text"]))
            expected = measurement["bbox"]
            if composed != expected:
                mismatches += 1
                if mismatches <= 10:
                    print(f"  MISMATCH {measurement['font']} {measurement['text']!r}: {expected} vs {composed}")
        # And for a large random sample drawn from every covered codepoint.
        import random

        random.seed(20260806)
        for key in FONT_FILES:
            font = generator._fonts[key]
            entry = glyph_metrics["fonts"][key]
            alphabet = [chr(int(cp)) for cp in entry["glyphs"]]
            for _ in range(2000):
                text = "".join(random.choice(alphabet) for _ in range(random.randint(1, 12)))
                if list(compose(entry, text)) != list(font.getbbox(text)):
                    mismatches += 1
                    if mismatches <= 20:
                        print(f"  RANDOM MISMATCH {key} {text!r}: {font.getbbox(text)} vs {compose(entry, text)}")
        print(f"composition mismatches: {mismatches}")
    finally:
        time_module.time = real_time
        datetime_module.datetime = original_datetime_class
        shutil.rmtree(shim, ignore_errors=True)


if __name__ == "__main__":
    main()
