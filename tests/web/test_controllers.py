from smarter_dev.web.controllers import _normalize_mounted_path


def test_normalize_mounted_path_strips_framework_added_trailing_slash() -> None:
    scope = {
        "path": "/quests/daily/current/",
        "raw_path": b"/api/quests/daily/current",
    }

    _normalize_mounted_path(scope)

    assert scope["path"] == "/quests/daily/current"


def test_normalize_mounted_path_preserves_client_trailing_slash() -> None:
    scope = {
        "path": "/guilds/644299523686006834/squads/",
        "raw_path": b"/api/guilds/644299523686006834/squads/",
    }

    _normalize_mounted_path(scope)

    assert scope["path"] == "/guilds/644299523686006834/squads/"


def test_normalize_mounted_path_leaves_root_unchanged() -> None:
    scope = {
        "path": "/",
        "raw_path": b"/api/",
    }

    _normalize_mounted_path(scope)

    assert scope["path"] == "/"
