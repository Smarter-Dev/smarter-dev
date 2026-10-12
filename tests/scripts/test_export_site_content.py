"""Contract for the static site's resource content export."""

import json
import re
from pathlib import Path

from jinja2 import Environment
from jinja2 import meta
from jsonschema import Draft202012Validator

from scripts.export_site_content import SOURCE_PAGE_DATA
from scripts.export_site_content import build_page_context
from scripts.export_site_content import export_site_content
from scripts.export_site_content import to_json_value
from smarter_dev.web import _resources_seed
from smarter_dev.web.resources_data import build_seed_directory_payload
from smarter_dev.web.vibe_courses_controller import build_vibe_courses_context

TEMPLATES = {
    "agentic-coding-courses": "vibe-coding-courses.html",
    **{
        slug: f"{slug}.html"
        for slug in SOURCE_PAGE_DATA
        if slug != "agentic-coding-courses"
    },
}
TEMPLATE_GLOBALS = {
    "_default_description",
    "_default_og_image",
    "_page_title",
    "csp_nonce",
    "mode",
    "page_type",
    "request",
    "static_url",
    "theme_url",
    "user",
    "r",
    "course",
}
RUNTIME_KEYS = {"popularity_unlocked"}


def _template_names(slug):
    root = Path(__file__).resolve().parents[2] / "themes/smarterdev/templates"
    environment = Environment()
    names = set()
    seen = set()

    def visit(template):
        if template in seen:
            return
        seen.add(template)
        parsed = environment.parse((root / template).read_text())
        names.update(meta.find_undeclared_variables(parsed))
        for child in meta.find_referenced_templates(parsed):
            if child is not None:
                visit(child)

    visit(TEMPLATES[slug])
    return names - TEMPLATE_GLOBALS


def _partial_fields(partial, variable):
    root = Path(__file__).resolve().parents[2] / "themes/smarterdev/templates"
    return set(re.findall(rf"\b{variable}\.(\w+)", (root / partial).read_text()))


def test_export_site_content_round_trips_and_validates(tmp_path):
    export_site_content(tmp_path)

    for slug in SOURCE_PAGE_DATA:
        document = json.loads((tmp_path / f"{slug}.json").read_text())
        assert document == to_json_value(build_page_context(slug))
        assert _template_names(slug) - RUNTIME_KEYS <= document.keys()
        assert not RUNTIME_KEYS & document.keys()
        sources = (
            document["tool_courses"] + document["workflow_courses"]
            if slug == "agentic-coding-courses"
            else document["spine"]
            + [
                source
                for category in document["category_resources"].values()
                for source in category
            ]
        )
        assert all("category_slug" in source for source in sources)
        assert all("click_count" not in source for source in sources)
        if slug == "agentic-coding-courses":
            required_fields = _partial_fields(
                "_partials/_vc-course-card.html", "course"
            )
            assert all("category" in source and "tools" in source for source in sources)
            assert document["featured_mode"] == "latest"
            assert document["featured"]
        else:
            required_fields = _partial_fields("_partials/_arch-resource-card.html", "r")
        assert all(
            required_fields - {"click_count"} <= source.keys() for source in sources
        )

    for path in tmp_path.glob("*.json"):
        schema = json.loads(
            (tmp_path / "schema" / f"{path.stem}.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema, format_checker=Draft202012Validator.FORMAT_CHECKER
        ).validate(json.loads(path.read_text()))

    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    }
    export_site_content(tmp_path)
    assert before == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    }
    assert all(blob.endswith(b"\n") for blob in before.values())


def test_export_site_content_catalog_matches_seed_links(tmp_path):
    export_site_content(tmp_path)
    catalog = json.loads((tmp_path / "resources-catalog.json").read_text())

    assert len(catalog["resource_directories"]) == len(SOURCE_PAGE_DATA) == 7
    assert {row["slug"] for row in catalog["resource_directories"]} == set(
        SOURCE_PAGE_DATA
    )
    assert (
        next(
            row
            for row in catalog["resource_categories"]
            if row["directory_slug"] == "agentic-coding-courses"
        )["sort_order"]
        == 10
    )

    source_keys = {row["track_key"] for row in catalog["resource_sources"]}
    assert len(source_keys) == len(catalog["resource_sources"])
    assert all(
        row["source_key"] in source_keys for row in catalog["resource_directory_spine"]
    )
    assert all(
        row["source_key"] in source_keys for row in catalog["resource_tool_sources"]
    )

    tool_keys = {
        (row["directory_slug"], row["slug"]) for row in catalog["resource_tools"]
    }
    assert all(
        (row["directory_slug"], row["tool_slug"]) in tool_keys
        for row in catalog["resource_tool_sources"]
    )


def test_export_site_content_live_builder_keeps_popularity_behavior():
    slug = "agentic-coding-courses"
    page = SOURCE_PAGE_DATA[slug]
    directory = next(row for row in _resources_seed._DIRECTORIES if row["slug"] == slug)
    keys = [course.key for course in page["courses"][:3]]
    counts = {key: 10 + index for index, key in enumerate(keys)}
    payload = build_seed_directory_payload(directory, page, counts)

    context = build_vibe_courses_context(payload)
    assert context["popularity_unlocked"] is True
    assert context["featured_mode"] == "popular"
    assert [course.key for course in context["featured"]] == keys[::-1]
