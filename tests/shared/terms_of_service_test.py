"""The Terms of Service wording (task #93).

The terms follow the privacy notice's house style: plain statements, sections
in a fixed order, no hedging and no bare domain in prose. These tests pin the
order, the commitments Zech asked for, and the governing law he chose.
"""

from __future__ import annotations

import re

import pytest

from smarter_dev.shared.terms_of_service import TERMS_PATH
from smarter_dev.shared.terms_of_service import terms_markdown

SECTIONS = (
    "Who we are and what these terms cover",
    "Eligibility",
    "Your account",
    "Acceptable use",
    "Content you post",
    "The AI assistant",
    "Paid subscriptions",
    "Moderation and termination",
    "Disclaimer and limitation of liability",
    "Privacy",
    "Changes",
    "Governing law",
    "Contact",
)


@pytest.fixture(scope="module")
def terms() -> str:
    return " ".join(terms_markdown().split())


def _section(name: str) -> str:
    return terms_markdown().split(f"## {name}\n", 1)[1].split("\n## ", 1)[0]


def test_the_sections_are_in_order():
    assert tuple(re.findall(r"^## (.+)$", terms_markdown(), re.MULTILINE)) == SECTIONS


def test_the_operator_and_scope_are_named(terms):
    who = _section("Who we are and what these terms cover")
    assert 'Smarter Dev LLC ("we")' in who
    assert "the Smarter Dev Discord server, its bot and our website, smarter.dev" in who


def test_the_bot_runs_in_one_server(terms):
    lowered = terms.lower()
    assert "servers" not in lowered
    assert "any server" not in lowered


def test_the_domain_is_never_bare_in_prose():
    for match in re.finditer(r"smarter\.dev", terms_markdown()):
        before = terms_markdown()[max(0, match.start() - 14) : match.start()]
        assert before.endswith(("our website, ", "admin@", "[admin@", "mailto:admin@"))


def test_eligibility_and_discords_terms():
    eligibility = _section("Eligibility")
    assert "13 or older" in eligibility
    assert "Discord's own [Terms of Service](https://discord.com/terms)" in eligibility
    assert "[Community Guidelines](https://discord.com/guidelines)" in eligibility


@pytest.mark.parametrize(
    ("section", "statement"),
    [
        ("Your account", "for one person"),
        ("Your account", "You are responsible for all activity under your account."),
        ("Acceptable use", "Abuse the bot or the AI assistant."),
        ("Acceptable use", "Circumvent moderation"),
        ("Acceptable use", "Scrape"),
        ("Content you post", "You own the content you post."),
        ("Content you post", "You must have the rights to everything you post."),
        ("The AI assistant", "The AI assistant can be wrong."),
        ("The AI assistant", "Nothing it says is professional advice"),
        ("Paid subscriptions", "You can cancel at any time."),
        (
            "Paid subscriptions",
            "takes effect at the end of the period you have paid for",
        ),
        (
            "Paid subscriptions",
            "We do not refund partial periods, except where the law requires it.",
        ),
        ("Moderation and termination", "at our discretion"),
        (
            "Moderation and termination",
            "You may leave the server or delete your account at any time.",
        ),
        ("Disclaimer and limitation of liability", "provided as is"),
        (
            "Disclaimer and limitation of liability",
            "the amount you paid us in the 12 months before the claim",
        ),
        ("Privacy", "[Privacy Policy](/privacy)"),
        ("Changes", "you accept the new terms"),
        ("Contact", "[admin@smarter.dev](mailto:admin@smarter.dev)"),
    ],
)
def test_each_section_makes_its_statement(section, statement):
    assert statement in " ".join(_section(section).split())


def test_privacy_is_one_sentence():
    assert _section("Privacy").strip().count(". ") == 0


def test_the_terms_are_governed_by_new_york_law():
    assert "the State of New York, United States" in _section("Governing law")
    assert "<STATE>" not in terms_markdown()


def test_the_terms_state_facts_without_hedging(terms):
    lowered = terms.lower()
    for hedge in (
        "about ",
        "up to",
        "approximately",
        "generally",
        "may be",
        "see above",
        "see below",
        "as described",
    ):
        assert hedge not in lowered


def test_the_path_is_the_one_the_portal_links():
    assert TERMS_PATH == "/terms"
