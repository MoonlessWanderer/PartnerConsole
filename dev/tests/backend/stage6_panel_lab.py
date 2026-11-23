"""Test-only response checkpoints for the local panel simulator."""
import asyncio
import os
from pathlib import Path

from v2lab.server import app as panel


async def app(scope, receive, send):
    if scope["type"] != "http" or not scope["path"].endswith("/user/update"):
        return await panel(scope, receive, send)

    checkpoint = Path(os.environ["STAGE6_CHECKPOINT"])
    armed = checkpoint / "armed"

    async def hold_response(message):
        if message["type"] == "http.response.start" and message["status"] == 200 and armed.exists():
            armed.unlink()
            (checkpoint / "applied").touch()
            # Keep the HTTP request open after the simulator has written its state.
            for _ in range(900):
                if (checkpoint / "release").exists():
                    break
                await asyncio.sleep(0.05)
        await send(message)

    await panel(scope, receive, hold_response)


_plan_reads = 0
_both_plans_read = asyncio.Event()


async def concurrent_plan_app(scope, receive, send):
    """When armed, hold the first two successful plan responses until both arrive."""
    if scope["type"] != "http" or not scope["path"].endswith("/plan/fetch"):
        return await panel(scope, receive, send)

    checkpoint = Path(os.environ["STAGE6_CHECKPOINT"])
    if not checkpoint.is_dir():
        return await panel(scope, receive, send)

    async def hold_plan_response(message):
        global _plan_reads
        if message["type"] == "http.response.start" and message["status"] == 200 and _plan_reads < 2:
            _plan_reads += 1
            if _plan_reads == 2:
                (checkpoint / "both-plan-reads").touch()
                _both_plans_read.set()
            await asyncio.wait_for(_both_plans_read.wait(), timeout=10)
        await send(message)

    await panel(scope, receive, hold_plan_response)
