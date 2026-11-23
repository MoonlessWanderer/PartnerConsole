"""A test-only panel that can withhold one successful update response."""
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
