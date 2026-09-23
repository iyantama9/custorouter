import json
import re

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

_THINK_OPEN = re.compile(r"<(think|thinking|thought|thoughts|thinking_process)\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</(think|thinking|thought|thoughts|thinking_process)\b[^>]*>", re.IGNORECASE)
_THINK_OPEN_PREFIXES = ("<think", "<thinking", "<thought", "<thoughts", "<thinking_process")
_THINK_CLOSE_PREFIXES = tuple("</" + prefix[1:] for prefix in _THINK_OPEN_PREFIXES)


def _partial_tag_tail_length(value: str, prefixes: tuple[str, ...]) -> int:
    """Length of a trailing partial tag that needs one more SSE chunk."""
    lower = value.lower()
    return max((size for prefix in prefixes for size in range(1, min(len(prefix), len(value)) + 1)
                if lower.endswith(prefix[:size])), default=0)


class _ThinkingStripper:
    """Remove reasoning tags across arbitrary SSE chunk boundaries."""

    def __init__(self):
        self.buffer = ""
        self.inside = False

    def feed(self, text: str, *, final: bool = False) -> str:
        self.buffer += text
        visible: list[str] = []
        while self.buffer:
            if self.inside:
                match = _THINK_CLOSE.search(self.buffer)
                if match:
                    self.buffer = self.buffer[match.end():]
                    self.inside = False
                    continue
                if final:
                    self.buffer = ""
                else:
                    # Retain only a real partial closing tag; holding an
                    # arbitrary 32 chars delays short responses noticeably.
                    tail = _partial_tag_tail_length(self.buffer, _THINK_CLOSE_PREFIXES)
                    self.buffer = self.buffer[-tail:] if tail else ""
                break

            match = _THINK_OPEN.search(self.buffer)
            if match:
                visible.append(self.buffer[:match.start()])
                self.buffer = self.buffer[match.end():]
                self.inside = True
                continue
            if final:
                visible.append(self.buffer)
                self.buffer = ""
            else:
                # Do not leak only the beginning of an actual tag split over
                # two chunks; ordinary short text should stream immediately.
                tail = _partial_tag_tail_length(self.buffer, _THINK_OPEN_PREFIXES)
                safe_length = len(self.buffer) - tail
                if safe_length:
                    visible.append(self.buffer[:safe_length])
                    self.buffer = self.buffer[safe_length:]
                break
        return "".join(visible)


def _anthropic_content_to_openai(content):
    """Translate Playground's upload blocks to the OpenAI chat shape."""
    if not isinstance(content, list):
        return content
    converted = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            converted.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64" and source.get("data"):
                converted.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{source.get('media_type', 'image/png')};base64,{source['data']}"},
                })
            elif source.get("type") == "url" and source.get("url"):
                converted.append({"type": "image_url", "image_url": {"url": source["url"]}})
        else:
            converted.append(block)
    return converted


def _openai_payload(payload: dict) -> dict:
    result = dict(payload)
    result["messages"] = [
        {**message, "content": _anthropic_content_to_openai(message.get("content"))}
        for message in payload.get("messages") or []
        if isinstance(message, dict)
    ]
    return result


def _strip_openai_response_reasoning(data: dict) -> dict:
    """Keep internal model reasoning out of Playground and its DB history."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list):
        return data
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        for key in ("reasoning", "reasoning_content", "reasoning_details", "analysis"):
            message.pop(key, None)
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = _ThinkingStripper().feed(content, final=True)
    return data


def _sanitize_openai_sse_line(line: str, stripper: _ThinkingStripper) -> list[str]:
    """Return OpenAI SSE lines safe to render/save, preserving SSE framing."""
    newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    bare = line[:-len(newline)] if newline else line
    if not bare.startswith("data: "):
        return [line]
    raw = bare[6:]
    if raw.strip() == "[DONE]":
        tail = stripper.feed("", final=True)
        generated = []
        if tail:
            generated.append(f'data: {json.dumps({"choices": [{"delta": {"content": tail}}]}, separators=(",", ":"))}\n\n')
        generated.append(line)
        return generated
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return [line]
    choices = event.get("choices")
    if not isinstance(choices, list):
        return [line]
    changed = False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for key in ("reasoning", "reasoning_content", "reasoning_details", "analysis"):
            if key in choice:
                choice.pop(key, None)
                changed = True
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        for key in ("reasoning", "reasoning_content", "reasoning_details", "analysis"):
            if key in delta:
                delta.pop(key, None)
                changed = True
        if isinstance(delta.get("content"), str):
            clean = stripper.feed(delta["content"])
            if clean != delta["content"]:
                delta["content"] = clean
                changed = True
    if not changed:
        return [line]
    return [f"data: {json.dumps(event, separators=(',', ':'))}{newline}"]


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

    # The dashboard speaks OpenAI shape internally. This preserves custom
    # provider parameters and avoids an unnecessary OpenAI→Anthropic→OpenAI
    # conversion for the interactive Playground.
    url = f"http://127.0.0.1:{PORT}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {ROUTER_PASSWORD}", "Content-Type": "application/json"}
    payload = _openai_payload(payload)

    if payload.get("stream", False):
        def _sse_error(message):
            payload_ = {"type": "error", "error": {"type": "api_error", "message": message}}
            return f"event: error\ndata: {json.dumps(payload_)}\n\n".encode()

        async def stream_generator():
            full_reply = ""
            buffer = ""
            thinking = _ThinkingStripper()
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
                        buffer += chunk.decode('utf-8', errors='ignore')
                        lines = buffer.splitlines(keepends=True)
                        if lines and not lines[-1].endswith(("\n", "\r")):
                            buffer = lines.pop()
                        else:
                            buffer = ""
                        for line in lines:
                            for safe_line in _sanitize_openai_sse_line(line, thinking):
                                yield safe_line.encode("utf-8")
                                if not safe_line.startswith('data: ') or safe_line.strip() == 'data: [DONE]':
                                    continue
                                try:
                                    d = json.loads(safe_line[6:])
                                    if d.get("choices") and d["choices"][0].get("delta", {}).get("content"):
                                        full_reply += d["choices"][0]["delta"]["content"]
                                except (json.JSONDecodeError, IndexError, TypeError):
                                    pass
                    if buffer:
                        for safe_line in _sanitize_openai_sse_line(buffer, thinking):
                            yield safe_line.encode("utf-8")
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
        data = _strip_openai_response_reasoning(data)
        reply_text = ""
        if "choices" in data and data["choices"]:
            reply_text = data["choices"][0].get("message", {}).get("content", "")
        elif "content" in data and isinstance(data["content"], list):
            reply_text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")

        if session_id and reply_text:
            await save_chat_message(session_id, "assistant", reply_text)
        return JSONResponse(content=data, status_code=resp.status_code)
