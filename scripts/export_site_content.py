"""Export the canonical Python resource content for the static site.

Run ``uv run python scripts/export_site_content.py [output-directory]``. The
catalog uses natural keys in place of database-generated UUIDs so it can be
created without Postgres and remain stable across runs.
"""

from __future__ import annotations

import argparse
import json
import types
from dataclasses import fields
from dataclasses import is_dataclass
from datetime import date
from pathlib import Path
from typing import Union
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from smarter_dev.web import _resources_seed
from smarter_dev.web import agent_engineering_patterns_data as agent
from smarter_dev.web import infrastructure_hosting_data as infra
from smarter_dev.web import patterns_of_practice_data as patterns
from smarter_dev.web import production_operations_data as ops
from smarter_dev.web import software_delivery_data as delivery
from smarter_dev.web import system_architecture_data as arch
from smarter_dev.web import vibe_courses_data as vibe

PAGE_DATA = {
    "agentic-coding-courses": {
        "categories": vibe.CATEGORIES,
        "tools": vibe.TOOLS,
        "courses": vibe.COURSES,
        "people": vibe.PEOPLE,
        "stay_current_platforms": sorted(vibe.STAY_CURRENT_PLATFORMS),
        "faqs": vibe.FAQS,
    },
    "system-architecture": {
        "categories": arch.ARCH_CATEGORIES,
        "spine": arch.ARCH_RESOURCES,
        "tool_resources": arch.ARCH_TOOL_RESOURCES,
        "people": arch.ARCH_PEOPLE,
        "faqs": arch.ARCH_FAQS,
        "learning_types": arch.CATEGORIES,
    },
    "infrastructure-hosting": {
        "categories": infra.INFRA_CATEGORIES,
        "spine": infra.INFRA_SPINE_RESOURCES,
        "tool_resources": infra.INFRA_TOOL_RESOURCES,
        "people": infra.INFRA_PEOPLE,
        "faqs": infra.INFRA_FAQS,
    },
    "software-delivery": {
        "categories": delivery.DELIV_CATEGORIES,
        "spine": delivery.DELIV_SPINE_RESOURCES,
        "tool_resources": delivery.DELIV_TOOL_RESOURCES,
        "people": delivery.DELIV_PEOPLE,
        "faqs": delivery.DELIV_FAQS,
    },
    "production-operations": {
        "categories": ops.OPS_CATEGORIES,
        "spine": ops.OPS_SPINE_RESOURCES,
        "tool_resources": ops.OPS_TOOL_RESOURCES,
        "people": ops.OPS_PEOPLE,
        "faqs": ops.OPS_FAQS,
    },
    "patterns-of-practice": {
        "categories": patterns.POP_CATEGORIES,
        "spine": patterns.POP_SPINE_RESOURCES,
        "tool_resources": patterns.POP_TOOL_RESOURCES,
        "people": patterns.POP_PEOPLE,
        "faqs": patterns.POP_FAQS,
        "sections_by_slug": patterns.POP_SECTIONS_BY_SLUG,
    },
    "agent-engineering-patterns": {
        "categories": agent.AGENT_CATEGORIES,
        "spine": agent.AGENT_SPINE_RESOURCES,
        "tool_resources": agent.AGENT_TOOL_RESOURCES,
        "people": agent.AGENT_PEOPLE,
        "faqs": agent.AGENT_FAQS,
    },
}


def to_json_value(value):
    """Preserve dataclass fields and sequence order; encode dates as ISO strings."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value]
    return value


def _schema_for_type(annotation):
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (types.UnionType, Union):
        return {"anyOf": [_schema_for_type(arg) for arg in args]}
    if annotation is type(None):
        return {"type": "null"}
    if annotation is date:
        return {"type": "string", "format": "date"}
    if annotation in (str, int, float, bool):
        return {
            "type": {str: "string", int: "integer", float: "number", bool: "boolean"}[
                annotation
            ]
        }
    if origin in (list, tuple):
        return {"type": "array", "items": _schema_for_type(args[0])}
    if origin is dict:
        return {"type": "object", "additionalProperties": _schema_for_type(args[1])}
    if is_dataclass(annotation):
        hints = get_type_hints(annotation)
        return {
            "type": "object",
            "properties": {
                field.name: _schema_for_type(hints[field.name])
                for field in fields(annotation)
            },
            "required": [field.name for field in fields(annotation)],
            "additionalProperties": False,
        }
    raise TypeError(f"Unsupported schema type: {annotation!r}")


def _schema_for_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _schema_for_type(type(value))
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {key: _schema_for_value(item) for key, item in value.items()},
            "required": list(value),
            "additionalProperties": False,
        }
    if isinstance(value, list | tuple):
        if not value:
            raise ValueError("Cannot infer schema from an empty sequence")
        if is_dataclass(value[0]) and all(
            type(item) is type(value[0]) for item in value
        ):
            item_schema = _schema_for_type(type(value[0]))
        else:
            variants = {}
            for item in value:
                schema = _schema_for_value(item)
                variants[json.dumps(schema, sort_keys=True)] = schema
            schemas = list(variants.values())
            item_schema = schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
        return {"type": "array", "items": item_schema}
    return _schema_for_type(type(value))


def _source_row(source):
    return {
        "track_key": source.key,
        "title": source.title,
        "url": source.url,
        "byline": source.source,
        "blurb": source.blurb or "",
        "learning_type": getattr(source, "learning_type", None) or source.category,
        "published_at": source.published_at,
        "first_indexed_at": source.first_indexed_at,
    }


def build_catalog():
    """Mirror the eight seeded tables with stable natural-key references."""
    catalog = {
        table: []
        for table in (
            "resource_directories",
            "resource_categories",
            "resource_tools",
            "resource_sources",
            "resource_directory_spine",
            "resource_tool_sources",
            "resource_creators",
            "resource_faqs",
        )
    }
    sources_by_key = {}
    for directory in _resources_seed._DIRECTORIES:
        slug = directory["slug"]
        page = PAGE_DATA[slug]
        catalog["resource_directories"].append(directory.copy())
        categories = page["categories"]
        if slug == "agentic-coding-courses":
            categories = [
                {
                    "slug": "agentic-tools",
                    "name": "Agentic Tools",
                    "intro": None,
                    "tools": page["tools"],
                }
            ]
        tool_slugs = set()
        for cat_index, category in enumerate(categories):
            cat = to_json_value(category)
            catalog["resource_categories"].append(
                {
                    "directory_slug": slug,
                    "slug": cat["slug"],
                    "name": cat["name"],
                    "intro_html": cat["intro"],
                    "sort_order": cat_index * 10,
                }
            )
            for tool_index, tool in enumerate(cat["tools"]):
                tool_slugs.add(tool["slug"])
                catalog["resource_tools"].append(
                    {
                        "directory_slug": slug,
                        "category_slug": cat["slug"],
                        "slug": tool["slug"],
                        "name": tool["name"],
                        "url": tool["url"],
                        "home_track_key": tool["home_key"],
                        "blurb": tool.get("blurb", tool.get("description", "")),
                        "sort_order": tool_index * 10,
                    }
                )
        if slug == "agentic-coding-courses":
            sources = page["courses"]
            spine = [item for item in sources if not item.tools]
            placed = [item for item in sources if item.tools]
        else:
            spine = page["spine"]
            placed = page["tool_resources"]
            sources = [*spine, *placed]
        for source in sources:
            row = _source_row(source)
            existing = sources_by_key.setdefault(source.key, row)
            if existing != row:
                raise ValueError(f"Conflicting source track key: {source.key}")
        for index, source in enumerate(spine):
            sort_order = (
                sources.index(source) if slug == "agentic-coding-courses" else index
            ) * 10
            catalog["resource_directory_spine"].append(
                {
                    "directory_slug": slug,
                    "source_key": source.key,
                    "sort_order": sort_order,
                }
            )
        for index, source in enumerate(placed):
            slugs = (
                source.tools if slug == "agentic-coding-courses" else source.tool_slugs
            )
            source_index = (
                sources.index(source) if slug == "agentic-coding-courses" else index
            )
            for position, tool_slug in enumerate(slugs):
                if tool_slug in tool_slugs:
                    catalog["resource_tool_sources"].append(
                        {
                            "directory_slug": slug,
                            "tool_slug": tool_slug,
                            "source_key": source.key,
                            "sort_order": source_index * 1000 + position,
                        }
                    )
        for index, person in enumerate(page["people"]):
            catalog["resource_creators"].append(
                {
                    "directory_slug": slug,
                    "slug": _resources_seed._creator_slug(person),
                    "name": person.name,
                    "handle": person.handle,
                    "platform": person.platform,
                    "url": person.url,
                    "track_key": person.key,
                    "blurb": person.blurb,
                    "sort_order": index * 10,
                }
            )
        for index, faq in enumerate(page["faqs"]):
            catalog["resource_faqs"].append(
                {
                    "directory_slug": slug,
                    "question": faq.question,
                    "answer": faq.answer,
                    "source_label": faq.source_label,
                    "source_url": faq.source_url,
                    "source_track_key": faq.source_key,
                    "sort_order": index * 10,
                }
            )
    catalog["resource_sources"] = list(sources_by_key.values())
    return catalog


def export_site_content(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    schema_dir = output / "schema"
    schema_dir.mkdir(exist_ok=True)
    documents = {**PAGE_DATA, "resources-catalog": build_catalog()}
    for name, document in documents.items():
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": name,
            **_schema_for_value(document),
        }
        (output / f"{name}.json").write_text(
            json.dumps(
                to_json_value(document), indent=2, sort_keys=True, ensure_ascii=False
            )
            + "\n"
        )
        (schema_dir / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=Path("content"))
    export_site_content(parser.parse_args().output)
