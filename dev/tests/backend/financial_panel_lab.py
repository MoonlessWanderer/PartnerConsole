"""Loopback browser fixture with observable upstream writes."""

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from v2lab.server import app, lab_users

write_count = 0
generate_count = 0
next_generate_failure = None
lost_replies = 0
users_at_drop = []
reset_requests = []
link_reset_ids = []


@app.middleware("http")
async def count_writes(request: Request, call_next):
    global write_count, generate_count, next_generate_failure, lost_replies, users_at_drop
    generate = request.method == "POST" and request.url.path.endswith("/user/generate")
    if generate:
        generate_count += 1
    if request.method == "POST" and "/user/" in request.url.path and request.url.path.startswith("/api/v1/"):
        write_count += 1
        if request.url.path.endswith("/user/update"):
            body = await request.json()
            if "u" in body or "d" in body:
                reset_requests.append({key: body.get(key) for key in ("id", "u", "d")})
        elif request.url.path.endswith("/user/resetSecret"):
            link_reset_ids.append((await request.json()).get("id"))
    response = await call_next(request)
    if next_generate_failure and generate and response.status_code == 200:
        failure = next_generate_failure
        next_generate_failure = None
        lost_replies += 1
        if failure == "disconnect":
            users_at_drop = [{"id": row["id"], "email": row["email"]} for row in lab_users()["data"]]
            # Close the connection before sending the promised response body.
            return Response(headers={"Content-Length": "1", "Connection": "close"})
        return JSONResponse({"message": "Synthetic reply lost after write"}, status_code=502)
    return response


@app.get("/_lab/consent-writes")
def writes():
    return {"count": write_count, "generate_count": generate_count}


@app.post("/_lab/consent-drop-next-generate")
def guard_next_generate(disconnect: bool = False):
    global next_generate_failure
    next_generate_failure = "disconnect" if disconnect else "proxy_error"
    return {"armed": True}


@app.get("/_lab/consent-lost-replies")
def dropped_replies():
    return {"count": lost_replies, "users_at_drop": users_at_drop}


@app.get("/_lab/traffic-resets")
def traffic_resets():
    return {"requests": reset_requests}


@app.get("/_lab/link-resets")
def link_resets():
    return {"ids": link_reset_ids}
