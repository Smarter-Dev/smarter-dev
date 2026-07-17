"""Local end-to-end smoke harness entry point.

    uv run python scripts/local_harness/run.py [--keep-up] [--skip-infra]

Pipeline: podman infra -> migrations (all four trees) -> seed -> boot app ->
readiness -> checks -> teardown. Non-zero exit if any check fails.

Flags:
    --keep-up    leave containers + app running after the checks (debugging);
                 prints the URLs and credentials needed to poke around.
    --skip-infra reuse already-running harness containers (implies the
                 databases are already migrated+seeded state you want).

See docs/v2/legacy-sunset/06-test-harness.md for how to update expectations.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.local_harness import config, infra  # noqa: E402
from scripts.local_harness.checks import run_all_checks  # noqa: E402
from scripts.local_harness.mock_discord import start_mock_discord_server  # noqa: E402


def run_migrations() -> None:
    """Apply all four migration trees against the harness databases."""
    result = subprocess.run(
        [sys.executable, "scripts/migrate.py"],
        cwd=str(REPO_ROOT),
        env=config.harness_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"migrations failed with exit code {result.returncode}")


def run_seed() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.local_harness.seed"],
        cwd=str(REPO_ROOT),
        env=config.harness_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"seeding failed with exit code {result.returncode}")


def boot_app() -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-m", "hypercorn",
            "scripts.local_harness.harness_app:app",
            "--bind", f"127.0.0.1:{config.APP_PORT}",
        ],
        cwd=str(REPO_ROOT),
        env=config.harness_env(),
    )


def wait_for_app(timeout_seconds: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{config.API_BASE_URL}/health", timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"status {response.status_code}"
        except httpx.HTTPError as error:
            last_error = str(error)
        time.sleep(0.5)
    raise RuntimeError(f"app did not become healthy in {timeout_seconds}s: {last_error}")


def print_keep_up_help() -> None:
    print(
        "\n--keep-up: everything left running for debugging\n"
        f"  app:           {config.APP_BASE_URL}\n"
        f"  api:           {config.API_BASE_URL} (Bearer {config.SKRIFT_BOT_API_KEY})\n"
        f"  main db:       {config.MAIN_DATABASE_URL}\n"
        f"  legacy db:     {config.LEGACY_DATABASE_URL}\n"
        f"  redis:         {config.REDIS_URL}\n"
        f"  mock discord:  {config.MOCK_DISCORD_BASE_URL}\n"
        f"  skrift admin:  log in at {config.APP_BASE_URL}/auth/dummy/login "
        "(any email, tick the admin box)\n"
        "  teardown:      podman rm -f "
        f"{config.POSTGRES_CONTAINER} {config.REDIS_CONTAINER} (app: kill hypercorn)\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-up", action="store_true",
                        help="leave infra + app running after the checks")
    parser.add_argument("--skip-infra", action="store_true",
                        help="reuse running harness containers; skip migrate+seed")
    args = parser.parse_args()

    app_process: subprocess.Popen | None = None
    mock_discord = None
    failed = False
    try:
        if not args.skip_infra:
            print("==> starting podman postgres + redis")
            infra.start_all()
            print("==> running migrations (skrift-main, main, skrift-legacy, legacy)")
            run_migrations()
            print("==> seeding representative data")
            run_seed()

        print("==> starting mock Discord API")
        mock_discord = start_mock_discord_server(config.MOCK_DISCORD_PORT)

        print("==> booting app (hypercorn, SKRIFT_ENV=development)")
        app_process = boot_app()
        wait_for_app()

        print("==> running checks\n")
        results = run_all_checks()
        for result in results:
            print(result.line())
        failures = [r for r in results if not r.passed]
        print(
            f"\n{len(results) - len(failures)}/{len(results)} checks passed"
            + (f", {len(failures)} FAILED" if failures else "")
        )
        failed = bool(failures)
    finally:
        if args.keep_up:
            if app_process is not None:
                print_keep_up_help()
                app_process.wait()
        else:
            if app_process is not None:
                app_process.terminate()
                try:
                    app_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    app_process.kill()
            if mock_discord is not None:
                mock_discord.shutdown()
            if not args.skip_infra:
                print("==> tearing down podman containers")
                infra.teardown_all()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
