"""Observe deleted users and exercise failure modes during recovery."""

import json
from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from v2lab.server import app, lab_users


armed_id = None
dropped_id = None
lookup_fault = None
delete_ids = []
lookups = []


@app.post("/_lab/drop-delete-reply/{user_id}")
def arm(user_id: int):
    global armed_id
    armed_id = user_id
    return {"armed": True}


@app.put("/_lab/delete-lookup-fault/{mode}")
def set_lookup_fault(mode: Literal["unavailable", "malformed"]):
    global lookup_fault
    lookup_fault = mode
    return {"fault": mode}


@app.delete("/_lab/delete-lookup-fault")
def clear_lookup_fault():
    global lookup_fault
    lookup_fault = None
    return {"fault": None}


@app.get("/_lab/delete-evidence")
def evidence():
    return {"delete_ids": delete_ids, "dropped_id": dropped_id, "lookups": lookups}


@app.middleware("http")
async def trace_deletion(request: Request, call_next):
    global armed_id, dropped_id
    path = request.url.path
    if request.method == "POST" and path.endswith("/user/delUser"):
        user_id = int((await request.json())["id"])
        delete_ids.append(user_id)
        was_present = any(row["id"] == user_id for row in lab_users()["data"])
        response = await call_next(request)
        if user_id == armed_id and response.status_code == 200 and was_present:
            body = b"".join([chunk async for chunk in response.body_iterator])
            assert json.loads(body) == {"data": True}
            assert all(row["id"] != user_id for row in lab_users()["data"])
            armed_id, dropped_id = None, user_id
            # Send no success body. The incomplete message closes the connection.
            return Response(headers={"Content-Length": "1", "Connection": "close"})
        return response

    if (dropped_id is not None and request.method == "GET" and path.endswith("/user/fetch")
            and request.query_params.get("filter[0][key]") == "id"):
        user_id = int(request.query_params["filter[0][value]"])
        if user_id == dropped_id and lookup_fault is not None:
            if lookup_fault == "unavailable":
                response = JSONResponse({"message": "Synthetic service unavailable"}, status_code=503)
            else:
                response = JSONResponse({"data": "not-a-list"})
            lookups.append({"user_id": user_id, "status": response.status_code, "ids": None, "fault": lookup_fault})
            return response
        response = await call_next(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        ids = [int(row["id"]) for row in json.loads(body)["data"]] if response.status_code == 200 else None
        lookups.append({"user_id": user_id, "status": response.status_code, "ids": ids})
        return Response(body, status_code=response.status_code, headers=dict(response.headers))
    return await call_next(request)
