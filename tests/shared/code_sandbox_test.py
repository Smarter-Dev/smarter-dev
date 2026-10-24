"""The run_code sandbox's limits, on the pydantic-monty this repo locks.

Each test runs real Monty through ``smarter_dev.web.chat.toolsets.run_code``,
the function smarter.dev's chat and the site's code endpoint both call, and
fails if its limit is removed: time, memory, sleep, filesystem, environment,
network and package imports, the output cap, and the per-process bound on
sandboxes running at once (which is what bounds their memory together).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from smarter_dev.shared import code_sandbox
from smarter_dev.web.chat.toolsets import MAX_CODE_OUTPUT_CHARS
from smarter_dev.web.chat.toolsets import MONTY_LIMITS
from smarter_dev.web.chat.toolsets import run_code

MIB = 1024 * 1024


async def _run(code: str, *, within: float = 30.0) -> tuple[str, float]:
    started = time.monotonic()
    text = await asyncio.wait_for(run_code("checking", code), timeout=within)
    return text, time.monotonic() - started


async def test_a_runaway_loop_stops_at_the_time_limit():
    text, took = await _run("while True:\n    pass\n")
    assert text.startswith("error: MontyRuntimeError: TimeoutError:"), text
    assert MONTY_LIMITS["max_feed_duration_secs"] <= took < 15


async def test_sleeping_does_not_hold_a_worker():
    # Sleep time is not execution time, so only the zero-sleep policy keeps a
    # sleeping snippet inside the time limit.
    text, took = await _run(
        "import time\nfor _ in range(30):\n    time.sleep(10)\n'woke'"
    )
    assert text == "return value: 'woke'"
    assert took < 5


async def test_a_large_allocation_stops_at_the_memory_limit():
    text, _ = await _run("x = 'a' * (300 * 1024 * 1024)\nlen(x)")
    assert text.startswith(
        "error: MontyRuntimeError: MemoryError: memory limit exceeded"
    ), text


async def test_a_growing_list_stops_at_the_memory_limit():
    text, _ = await _run("x = []\nwhile True:\n    x.append('abcdefgh' * 100)\n")
    assert text.startswith(
        "error: MontyRuntimeError: MemoryError: memory limit exceeded"
    ), text


@pytest.mark.parametrize(
    "code",
    [
        "open('/etc/hostname').read()",
        "import os\nos.listdir('/')",
        "import pathlib\npathlib.Path('/etc/hostname').read_text()",
    ],
)
async def test_the_filesystem_cannot_be_read(code):
    text, _ = await _run(code)
    assert text.startswith("error: MontyRuntimeError: PermissionError:"), text


async def test_the_filesystem_cannot_be_written(tmp_path):
    target = tmp_path / "written.txt"
    text, _ = await _run(f"open({str(target)!r}, 'w').write('x')")
    assert text.startswith("error: MontyRuntimeError: PermissionError:"), text
    assert not target.exists()


@pytest.mark.parametrize(
    "code", ["import os\nos.environ", "import os\nos.getenv('HOME')"]
)
async def test_the_environment_cannot_be_read(code):
    text, _ = await _run(code)
    assert text.startswith("error: MontyRuntimeError: RuntimeError:"), text
    assert "not supported" in text


@pytest.mark.parametrize(
    "module", ["socket", "urllib", "http", "ssl", "asyncio.streams", "subprocess"]
)
async def test_nothing_that_reaches_the_network_or_a_process_imports(module):
    text, _ = await _run(f"import {module}")
    assert text.startswith("error: MontyRuntimeError: ModuleNotFoundError:"), text


@pytest.mark.parametrize("module", ["httpx", "pydantic", "pydantic_monty", "ctypes"])
async def test_installed_packages_do_not_import(module):
    # These are installed in the host's environment; the sandbox cannot see it.
    text, _ = await _run(f"import {module}")
    assert text.startswith("error: MontyRuntimeError: ModuleNotFoundError:"), text


async def test_output_is_capped_before_the_model_reads_it():
    text, _ = await _run("print('a' * 50_000)\n7")
    assert len(text) == MAX_CODE_OUTPUT_CHARS
    assert text.startswith("stdout:\naaaa")


async def test_output_before_an_error_is_capped_too():
    text, _ = await _run("print('a' * 50_000)\n1 / 0")
    assert len(text) == MAX_CODE_OUTPUT_CHARS
    assert text.startswith("error: MontyRuntimeError: ZeroDivisionError")


def _monty_children() -> dict[int, int]:
    """RSS in bytes of each live ``monty`` worker this process spawned."""
    me = os.getpid()
    out: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            comm = stat[stat.index("(") + 1 : stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
            if comm != "monty" or ppid != me:
                continue
            status = (entry / "status").read_text()
        except (OSError, ValueError):
            continue
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                out[int(entry.name)] = int(line.split()[1]) * 1024
    return out


HOG = "x = []\nwhile True:\n    x.append('abcdefgh' * 100)\n"


async def _hogs(count: int) -> tuple[list[str], int, int]:
    """``count`` members filling their memory cap at once: each one's text,
    the most workers alive at once and their most RSS together."""
    peak_workers = 0
    peak_rss = 0
    done = False

    async def sample():
        nonlocal peak_workers, peak_rss
        while not done:
            children = _monty_children()
            peak_workers = max(peak_workers, len(children))
            peak_rss = max(peak_rss, sum(children.values()))
            await asyncio.sleep(0.005)

    sampler = asyncio.create_task(sample())
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(run_code("hog", HOG) for _ in range(count))), timeout=180
        )
    finally:
        done = True
        await sampler
    return results, peak_workers, peak_rss


@pytest.mark.parametrize(
    ("budget", "cap", "workers"),
    [
        # Each deployment's budget (k8s/), and the default.
        (128, 112, 1),  # the bot: one run, its cap cut to fit
        (288, 256, 1),  # the default: one chat-size run
        (300, 256, 1),  # chat-child-worker
        (400, 256, 1),  # agent-worker: one chat-size run (and room for fires)
        (560, 256, 2),
    ],
)
async def test_parallel_runs_never_hold_more_than_the_budget(
    monkeypatch, budget, cap, workers
):
    # Six members each fill the memory limit at once. Without the budget, six
    # workers near 256 MiB would coexist (about 1.6 GiB in one pod).
    monkeypatch.setenv(code_sandbox.BUDGET_ENV, str(budget))
    results, peak_workers, peak_rss = await _hogs(6)
    assert all(
        "MemoryError: memory limit exceeded: " in text
        and f"> {cap * MIB} bytes" in text
        for text in results
    ), results[0]
    assert peak_workers == workers
    assert peak_rss > (cap - 16) * MIB  # the sampler saw a hog near its cap
    assert peak_rss <= budget * MIB


@pytest.mark.parametrize("cap", [16, 112, 256])
async def test_a_worker_at_its_cap_stays_within_the_overhead(monkeypatch, cap):
    # What the budget reserves per run beyond its cap.
    monkeypatch.setenv(
        code_sandbox.BUDGET_ENV, str(cap + code_sandbox.WORKER_OVERHEAD_MIB)
    )
    _, _, peak_rss = await _hogs(1)
    assert peak_rss <= (cap + code_sandbox.WORKER_OVERHEAD_MIB) * MIB


async def test_the_budget_grants_in_arrival_order():
    budget = code_sandbox._Budget(100)
    order: list[str] = []

    async def hold(name: str, size: int, release: asyncio.Event):
        async with budget.reserve(size):
            order.append(name)
            await release.wait()

    first, big, small = asyncio.Event(), asyncio.Event(), asyncio.Event()
    a = asyncio.create_task(hold("first", 60, first))
    await asyncio.sleep(0)
    b = asyncio.create_task(hold("big", 80, big))
    await asyncio.sleep(0)
    c = asyncio.create_task(hold("small", 20, small))
    await asyncio.sleep(0.01)
    # 20 MiB would fit beside the first, but the big run arrived before it.
    assert order == ["first"]
    first.set()
    await asyncio.sleep(0.01)
    # Once the first is done, the big one goes first; the small one fits
    # beside it.
    assert order == ["first", "big", "small"]
    assert budget.used == 100
    big.set()
    small.set()
    await asyncio.gather(a, b, c)
    assert budget.used == 0


async def test_a_cancelled_wait_gives_its_place_back():
    budget = code_sandbox._Budget(100)
    holding = asyncio.Event()

    async def hold():
        async with budget.reserve(100):
            await holding.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(budget.reserve(50).__aenter__())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    holding.set()
    await holder
    assert budget.used == 0
    async with budget.reserve(100):
        assert budget.used == 100


async def test_a_worker_exits_when_its_run_ends():
    # A worker that peaked at the memory limit must not linger holding it.
    await _run("x = [0] * 10_000_000\nlen(x)")
    assert _monty_children() == {}
