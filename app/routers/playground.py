import json

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from app.config import ROUTER_PASSWORD, PORT
from app.database import (
    get_chat_sessions, get_chat_messages, create_chat_session,
    update_chat_session, delete_chat_session, save_chat_message,
)
from app.routers.admin import require_auth
from app.routers.proxy import get_shared_http_client


router = APIRouter()


@router.get("/api/playground/sessions")
async def api_get_sessions(user: None = Depends(require_auth)):
    rows = await get_chat_sessions()
    return [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None} for r in rows]


@router.post("/api/playground/sessions")
async def api_create_session(request: Request, user: None = Depends(require_auth)):
    payload = await request.json()
    name = payload.get("name", "New Chat")
    row = await create_chat_session(name)
    return {"id": row["id"], "name": row["name"]}


@router.put("/api/playground/sessions/{session_id}")
async def api_update_session(session_id: int, request: Request, user: None = Depends(require_auth)):
    payload = await request.json()
    name = payload.get("name")
    if name:
        await update_chat_session(session_id, name)
    return {"success": True}


@router.delete("/api/playground/sessions/{session_id}")
async def api_delete_session(session_id: int, user: None = Depends(require_auth)):
    await delete_chat_session(session_id)
    return {"success": True}


@router.get("/api/playground/sessions/{session_id}/messages")
async def api_get_session_messages(session_id: int, user: None = Depends(require_auth)):
    rows = await get_chat_messages(session_id)
    return [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in rows]


@router.post("/api/playground/chat")
async def api_playground_chat(request: Request, user: None = Depends(require_auth)):
    payload = await request.json()
    session_id = payload.pop("session_id", None)

    if session_id and payload.get("messages"):
        last_msg = payload["messages"][-1]
        if last_msg["role"] == "user":
            content_str = last_msg["content"]
            if isinstance(content_str, list):
                content_str = json.dumps(content_str)
            await save_chat_message(session_id, "user", str(content_str))

    url = f"http://127.0.0.1:{PORT}/v1/messages"
    headers = {"Authorization": f"Bearer {ROUTER_PASSWORD}", "Content-Type": "application/json"}

    if payload.get("stream", False):
        def _sse_error(message):
            payload_ = {"type": "error", "error": {"type": "api_error", "message": message}}
            return f"event: error\ndata: {json.dumps(payload_)}\n\n".encode()

        async def stream_generator():
            full_reply = ""
            buffer = ""
            client = get_shared_http_client()
            async with client.stream("POST", url, headers=headers, json=payload, timeout=300.0) as response:
                # A StreamingResponse commits to 200 before the router has
                # answered, so an upstream failure has to be handed to the
                # browser as an SSE error event.
                if response.status_code != 200:
                    raw = (await response.aread()).decode(errors="replace")
                    try:
                        parsed = json.loads(raw)
                        message = (parsed.get("error") or {}).get("message") or raw
                    except Exception:
                        message = raw
                    yield _sse_error(message or f"HTTP {response.status_code}")
                    return

                try:
                    async for chunk in response.aiter_raw():
                        yield chunk
                        buffer += chunk.decode('utf-8', errors='ignore')
                        lines = buffer.split('\n')
                        buffer = lines.pop()
                        for line in lines:
                            if not line.startswith('data: ') or line == 'data: [DONE]':
                                continue
                            try:
                                d = json.loads(line[6:])
                                if d.get("choices") and d["choices"][0].get("delta", {}).get("content"):
                                    full_reply += d["choices"][0]["delta"]["content"]
                                elif d.get("type") == "content_block_delta" and d.get("delta", {}).get("text"):
                                    full_reply += d["delta"]["text"]
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[PLAYGROUND] Stream failed: {type(e).__name__}: {e}")
                    yield _sse_error("The upstream stream ended unexpectedly.")

            if session_id and full_reply:
                await save_chat_message(session_id, "assistant", full_reply)

        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    else:
        client = get_shared_http_client()
        resp = await client.post(url, headers=headers, json=payload, timeout=300.0)
        try:
            data = resp.json()
        except Exception:
            data = {"error": {"message": "Router returned an invalid response."}}
        reply_text = ""
        if "choices" in data and data["choices"]:
            reply_text = data["choices"][0].get("message", {}).get("content", "")
        elif "content" in data and isinstance(data["content"], list):
            reply_text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")

        if session_id and reply_text:
            await save_chat_message(session_id, "assistant", reply_text)
        return JSONResponse(content=data, status_code=resp.status_code)
