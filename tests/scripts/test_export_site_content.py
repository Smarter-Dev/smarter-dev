"""Contract for the static site's resource content export."""

import json

from jsonschema import Draft202012Validator

from scripts.export_site_content import PAGE_DATA
from scripts.export_site_content import export_site_content
from scripts.export_site_content import to_json_value


def test_export_site_content_round_trips_and_validates(tmp_path):
    export_site_content(tmp_path)

    for slug, source in PAGE_DATA.items():
        document = json.loads((tmp_path / f"{slug}.json").read_text())
        assert document == to_json_value(source)

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

    assert len(catalog["resource_directories"]) == len(PAGE_DATA) == 7
    assert {row["slug"] for row in catalog["resource_directories"]} == set(PAGE_DATA)

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
