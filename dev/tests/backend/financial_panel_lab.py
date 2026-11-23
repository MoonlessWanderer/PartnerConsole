"""Loopback browser fixture with observable upstream writes."""

from fastapi import Request
from fastapi.responses import JSONResponse
from v2lab.server import app

write_count = 0
drop_generate = False
lost_replies = 0


@app.middleware("http")
async def count_writes(request: Request, call_next):
    global write_count, drop_generate, lost_replies
    if request.method == "POST" and "/user/" in request.url.path and request.url.path.startswith("/api/v1/"):
        write_count += 1
    response = await call_next(request)
    if drop_generate and request.url.path.endswith("/user/generate") and response.status_code == 200:
        drop_generate = False
        lost_replies += 1
        return JSONResponse({"message": "Synthetic reply lost after write"}, status_code=502)
    return response


@app.get("/_lab/consent-writes")
def writes():
    return {"count": write_count}


@app.post("/_lab/consent-drop-next-generate")
def guard_next_generate():
    global drop_generate
    drop_generate = True
    return {"armed": True}


@app.get("/_lab/consent-lost-replies")
def dropped_replies():
    return {"count": lost_replies}
