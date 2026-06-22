from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT)]

from drivers.imessage import inbound  # noqa: E402


def test_live_quiet_wait_coalesces_until_quiet() -> None:
    async def run() -> float:
        queue: asyncio.Queue = asyncio.Queue()

        async def add_wakeups() -> None:
            await asyncio.sleep(0.01)
            await queue.put(None)
            await asyncio.sleep(0.02)
            await queue.put(None)

        task = asyncio.create_task(add_wakeups())
        loop = asyncio.get_running_loop()
        started = loop.time()
        await inbound._wait_for_live_quiet(queue, quiet_s=0.05, max_s=0.25)
        await task
        assert queue.empty()
        return loop.time() - started

    elapsed = asyncio.run(run())

    assert elapsed >= 0.07
    assert elapsed < 0.18


def test_live_quiet_wait_honors_max_cap() -> None:
    async def run() -> float:
        queue: asyncio.Queue = asyncio.Queue()

        async def keep_waking() -> None:
            while True:
                await asyncio.sleep(0.01)
                await queue.put(None)

        task = asyncio.create_task(keep_waking())
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await inbound._wait_for_live_quiet(queue, quiet_s=0.05, max_s=0.04)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return loop.time() - started

    elapsed = asyncio.run(run())

    assert elapsed >= 0.035
    assert elapsed < 0.10
