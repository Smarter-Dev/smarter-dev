"""Podman-backed throwaway postgres + redis for the smoke harness.

Containers use unique names/ports and no named volumes, so they can never
collide with (or corrupt) a developer stack; teardown is ``podman rm -f``.
"""

from __future__ import annotations

import subprocess
import time

from scripts.local_harness import config


class InfraError(RuntimeError):
    """Raised when harness infrastructure fails to start."""


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _remove_container(name: str) -> None:
    _run(["podman", "rm", "-f", name], check=False)


def start_postgres() -> None:
    """Start the harness postgres with both databases (via repo init scripts)."""
    _remove_container(config.POSTGRES_CONTAINER)
    init_dir = config.REPO_ROOT / "scripts" / "postgres-init"
    _run(
        [
            "podman", "run", "-d", "--rm",
            "--name", config.POSTGRES_CONTAINER,
            "-e", f"POSTGRES_DB={config.MAIN_DB_NAME}",
            "-e", f"POSTGRES_USER={config.DB_USER}",
            "-e", f"POSTGRES_PASSWORD={config.DB_PASSWORD}",
            "-p", f"{config.POSTGRES_PORT}:5432",
            "-v", f"{init_dir}:/docker-entrypoint-initdb.d:ro",
            config.POSTGRES_IMAGE,
        ]
    )


def start_redis() -> None:
    _remove_container(config.REDIS_CONTAINER)
    _run(
        [
            "podman", "run", "-d", "--rm",
            "--name", config.REDIS_CONTAINER,
            "-p", f"{config.REDIS_PORT}:6379",
            config.REDIS_IMAGE,
            "redis-server", "--requirepass", config.REDIS_PASSWORD,
        ]
    )


def wait_for_postgres(timeout_seconds: float = 60.0) -> None:
    """Wait until both databases accept connections (init scripts done)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        legacy_ready = _run(
            [
                "podman", "exec", config.POSTGRES_CONTAINER,
                "psql", "-U", config.DB_USER, "-d", config.LEGACY_DB_NAME,
                "-c", "SELECT 1",
            ],
            check=False,
        )
        if legacy_ready.returncode == 0:
            return
        time.sleep(0.5)
    raise InfraError(
        f"postgres ({config.POSTGRES_CONTAINER}) not ready in {timeout_seconds}s"
    )


def wait_for_redis(timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ping = _run(
            [
                "podman", "exec", config.REDIS_CONTAINER,
                "redis-cli", "-a", config.REDIS_PASSWORD, "ping",
            ],
            check=False,
        )
        if ping.returncode == 0 and "PONG" in ping.stdout:
            return
        time.sleep(0.5)
    raise InfraError(f"redis ({config.REDIS_CONTAINER}) not ready in {timeout_seconds}s")


def start_all() -> None:
    start_postgres()
    start_redis()
    wait_for_postgres()
    wait_for_redis()


def teardown_all() -> None:
    _remove_container(config.POSTGRES_CONTAINER)
    _remove_container(config.REDIS_CONTAINER)
