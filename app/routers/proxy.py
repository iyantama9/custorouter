import json
import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

import app.config as config_module
from app.config import (
    BLUESMINDS_API_KEY, BLUESMINDS_BASE_URL,
    ROUTER_PASSWORD, NARA_BASE_URL, DAHL_BASE_URL, QWEN_CLOUD_BASE_URL, MARKETKU_BASE_URL,
    resolve_dahl_model, BM_API_KEYS, NR_API_KEYS, DAHL_API_KEYS, QC_API_KEYS, MARKETKU_API_KEYS,
    get_current_bm_key, rotate_bm_key, get_current_nr_key, rotate_nr_key, get_current_dahl_key, rotate_dahl_key, get_current_marketku_key, rotate_marketku_key,
    get_current_qc_key_for_model, rotate_qc_key_for_model, mark_qc_model_exhausted,
    recent_requests, add_request_log,
)
from app.translator import (
    build_openai_request, to_anthropic_response, stream_as_anthropic, compact_messages,
    is_context_window_error, to_anthropic_stream_error, estimate_tokens,
)
from app.translator_openai import (
    openai_to_anthropic_messages, anthropic_to_openai_response, make_anthropic_to_openai_stream_converter,
    openai_tools_to_anthropic, openai_tool_choice_to_anthropic,
)
from app.sse import sse_broadcaster


logger = logging.getLogger(__name__)
from app.database import verify_router_api_key, add_router_key_token_usage


router = APIRouter()
MAX_REQUEST_BODY_BYTES = max(1024, int(os.getenv("MAX_REQUEST_BODY_BYTES", str(25 * 1024 * 1024))))
_RETRYABLE_UPSTREAM_STATUSES = {401, 402, 403, 404, 429, 500, 502, 503, 504}


def _should_retry_custom_key(status_code: int, body) -> bool:
    """Retry another key for transient failures or quota-like HTTP 400s."""
    if status_code in _RETRYABLE_UPSTREAM_STATUSES:
        return True
    if status_code != 400:
        return False
    try:
        detail = json.dumps(body, ensure_ascii=False).lower()[:4096]
    except (TypeError, ValueError):
        detail = str(body).lower()[:4096]
    quota_terms = (
        "out of credit", "out_of_credit", "out of credits",
        "insufficient credit", "insufficient balance", "credits exhausted",
        "credit exhausted", "no credits left", "quota exceeded",
        "quota_exceeded", "insufficient_quota", "billing limit",
        "余额不足", "积分不足",
    )
    return any(term in detail for term in quota_terms)


def _is_qc_model_quota_error(status_code: int, body) -> bool:
    """Only model quota failures permanently exclude a QC key/model pair."""
    if status_code in (402, 429):
        return True
    try:
        detail = json.dumps(body, ensure_ascii=False).lower()
    except Exception:
        detail = str(body).lower()
    quota_terms = (
        "quota", "rate limit", "rate_limit", "insufficient balance",
        "resource exhausted", "allocation exhausted",
    )
    return any(term in detail for term in quota_terms)


def _rotate_qc_after_failure(model: str, key: str, status_code: int, body) -> bool:
    if _is_qc_model_quota_error(status_code, body):
        mark_qc_model_exhausted(key, model)
    return rotate_qc_key_for_model(model, after_key=key)

_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
_CUSTOM_PROVIDER_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=15.0, pool=10.0)
_HTTP_LIMITS = httpx.Limits(
    max_connections=max(10, int(os.getenv("HTTP_MAX_CONNECTIONS", "200"))),
    max_keepalive_connections=max(5, int(os.getenv("HTTP_MAX_KEEPALIVE_CONNECTIONS", "50"))),
    keepalive_expiry=30.0,
)
_upstream_client: httpx.AsyncClient | None = None
_custom_client: httpx.AsyncClient | None = None


async def init_http_clients():
    """Create process-wide pools so upstream TLS connections are reused."""
    global _upstream_client, _custom_client
    if _upstream_client is None or _upstream_client.is_closed:
        _upstream_client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT, limits=_HTTP_LIMITS)
    if _custom_client is None or _custom_client.is_closed:
        _custom_client = httpx.AsyncClient(timeout=_CUSTOM_PROVIDER_TIMEOUT, limits=_HTTP_LIMITS)


async def close_http_clients():
    global _upstream_client, _custom_client
    clients = [client for client in (_upstream_client, _custom_client) if client and not client.is_closed]
    for client in clients:
        await client.aclose()
    _upstream_client = None
    _custom_client = None


def _get_upstream_client() -> httpx.AsyncClient:
    if _upstream_client is None or _upstream_client.is_closed:
        raise RuntimeError("Upstream HTTP client is not initialized")
    return _upstream_client


def get_shared_http_client() -> httpx.AsyncClient:
    """Shared client for same-process helpers such as the Playground proxy."""
    return _get_upstream_client()


def _get_custom_client() -> httpx.AsyncClient:
    if _custom_client is None or _custom_client.is_closed:
        raise RuntimeError("Custom-provider HTTP client is not initialized")
    return _custom_client


@asynccontextmanager
async def _borrow_upstream_client():
    yield _get_upstream_client()


@asynccontextmanager
async def _borrow_custom_client():
    yield _get_custom_client()


async def _build_status_dict(include_details: bool = True):
    uptime_seconds = int(time.time() - config_module.START_TIME)
    if include_details:
        from app.config import get_masked_keys
        all_keys = get_masked_keys()
        total_keys = len(all_keys)
        available_keys = sum(k['status'] in ('Active', 'Standby') for k in all_keys)
    else:
        key_values = (config_module.BM_API_KEYS + config_module.NR_API_KEYS +
                      config_module.DAHL_API_KEYS + config_module.QC_API_KEYS +
                      config_module.MARKETKU_API_KEYS)
        key_values += [key for keys in config_module.CUSTOM_PROVIDER_KEYS.values() for key in keys]
        total_keys = len(key_values)
        available_keys = sum(config_module.key_statuses.get(key, 'Standby') in ('Active', 'Standby') for key in key_values)
    status = {
        "status": "online",
        "uptime_seconds": uptime_seconds,
        "total_requests": config_module.total_requests,
        "failover_count": config_module.failover_count,
        "total_tokens": config_module.total_tokens,
        "available_keys": available_keys,
        "total_keys": total_keys,
    }
    if include_details:
        status.update({
            "keys": all_keys,
            "recent_requests": recent_requests,
            "providers_signature": config_module.providers_signature(),
        })
    return status


async def _check_router_auth(request: Request):
    """Check router authentication via password or API key."""
    auth_header = request.headers.get("Authorization")
    x_api_key = request.headers.get("x-api-key")

    # Extract token from header
    token = None
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    elif x_api_key:
        token = x_api_key

    if not token:
        return False

    # Check ROUTER_PASSWORD first (backward compatibility)
    if ROUTER_PASSWORD and hmac.compare_digest(token, ROUTER_PASSWORD):
        return True

    # Check router API keys from database
    if token.startswith("rtr_"):
        key_row = await verify_router_api_key(token)
        if not key_row:
            return False
        # Stash the row so downstream code can enforce this key's model
        # allowlist and bill its token quota.
        request.state.router_key = key_row
        return True

    return False


async def _read_json_payload(request: Request):
    """Read a JSON object with an early size guard and client-safe errors."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BODY_BYTES:
                return None, JSONResponse(
                    status_code=413,
                    content={"error": {"message": "Request body is too large."}},
                )
        except ValueError:
            return None, JSONResponse(
                status_code=400,
                content={"error": {"message": "Invalid Content-Length header."}},
            )
    try:
        payload = await request.json()
    except Exception:
        return None, JSONResponse(
            status_code=400,
            content={"error": {"message": "Request body must be valid JSON."}},
        )
    if not isinstance(payload, dict):
        return None, JSONResponse(
            status_code=400,
            content={"error": {"message": "Request body must be a JSON object."}},
        )
    return payload, None


def _router_key(request: Request):
    return getattr(request.state, "router_key", None)


def _model_allowed_for_key(request: Request, model: str):
    """
    Enforce a router key's model allowlist. Returns None when allowed, or an
    error message. An empty allowlist means the key may use every model.
    """
    key_row = _router_key(request)
    if not key_row:
        return None
    raw = (key_row.get("allowed_models") or "").strip()
    if not raw:
        return None
    allowed = {m.strip() for m in raw.split(",") if m.strip()}
    if model in allowed:
        return None
    shown = sorted(allowed)[:10]
    return (
        f"This API key is not allowed to use model '{model}'. "
        f"Allowed: {', '.join(shown)}{'...' if len(allowed) > 10 else ''}"
    )


def _key_aliases(request: Request) -> dict:
    """{real model id: alias} configured on the router key making this request."""
    key_row = _router_key(request)
    if not key_row:
        return {}
    raw = key_row.get("model_aliases")
    if not raw:
        return {}
    try:
        aliases = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}
    return aliases if isinstance(aliases, dict) else {}


def _resolve_alias(request: Request, requested: str):
    """
    Map an aliased model name back to the real one.

    Returns (real_model, display_model). display_model is what the caller
    asked for, so responses can echo the alias back rather than leaking the
    model actually behind it.
    """
    aliases = _key_aliases(request)
    if not aliases:
        return requested, requested
    for real, alias in aliases.items():
        if alias == requested:
            return real, alias
    return requested, aliases.get(requested, requested)


def _key_model_prompt(request: Request, model: str) -> str:
    """
    The per-model system prompt configured on the router key making this
    request, if any. Scoped to that key -- the same model called with a
    different key, or with the router password, gets nothing.
    """
    key_row = _router_key(request)
    if not key_row:
        return ""
    raw = key_row.get("model_prompts")
    if not raw:
        return ""
    try:
        prompts = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if not isinstance(prompts, dict):
        return ""
    return str(prompts.get(model) or "").strip()


def _builtin_provider_for_model(model: str) -> str:
    """Resolve a built-in provider once, for both API protocol routes.

    Keeping this shared matters because Playground uses the Anthropic route,
    while most external clients use the OpenAI route.  They must never route
    the same prefixed model differently.
    """
    if model.startswith("bm/") or model in config_module.BLUESMINDS_MODELS:
        return "bm"
    if model.startswith("nry/") or model in config_module.NARA_MODELS:
        return "nry"
    if model.startswith("dh/") or model in config_module.DAHL_MODELS_SHORT:
        return "dahl"
    if model.startswith("qc/"):
        return "qc"
    if model.startswith("mk/") or model in config_module.MARKETKU_MODELS:
        return "marketku"
    return "bm"


def _inject_anthropic_system(payload: dict, prompt: str):
    """Prepend a system prompt to an Anthropic-shaped payload in place."""
    if not prompt:
        return
    existing = payload.get("system")
    if isinstance(existing, list):
        payload["system"] = [{"type": "text", "text": prompt}] + existing
    elif isinstance(existing, str) and existing.strip():
        payload["system"] = f"{prompt}\n\n{existing}"
    else:
        payload["system"] = prompt


def _inject_openai_system(payload: dict, prompt: str):
    """Prepend a system prompt to an OpenAI-shaped payload in place."""
    if not prompt:
        return
    messages = list(payload.get("messages") or [])
    idx = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)
    if idx is None:
        messages.insert(0, {"role": "system", "content": prompt})
    else:
        existing = messages[idx].get("content")
        if isinstance(existing, str) and existing.strip():
            messages[idx] = {**messages[idx], "content": f"{prompt}\n\n{existing}"}
        else:
            messages[idx] = {**messages[idx], "content": prompt}
    payload["messages"] = messages


async def _bill_router_key(request: Request, tokens: int):
    """Charge a request's tokens against the router key that made it."""
    key_row = _router_key(request)
    if not key_row or tokens <= 0:
        return
    try:
        await add_router_key_token_usage(key_row["id"], tokens)
    except Exception as e:
        print(f"[ROUTER-KEY] Failed to record token usage: {e}", flush=True)




async def _broadcast_request_log():
    """Push the just-recorded request to connected dashboards (live log + the
    routing graph's connector animation)."""
    try:
        await sse_broadcaster.broadcast("log", recent_requests[0] if recent_requests else {})
        await sse_broadcaster.broadcast("status", await _build_status_dict(include_details=False))
    except Exception:
        pass


def _extract_cached_tokens(openai_resp: dict) -> int:
    """
    Pull prompt-cache hits out of an OpenAI-shaped usage block.

    Providers disagree on where this lives: OpenAI nests it under
    prompt_tokens_details, others put cached_tokens at the usage root.
    Check both and fall back to 0 rather than guessing.
    """
    usage = (openai_resp or {}).get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    for candidate in (details.get("cached_tokens"), usage.get("cached_tokens")):
        if isinstance(candidate, (int, float)):
            return int(candidate)
    return 0


def _update_anthropic_stream_usage(event: dict, tracker: dict):
    """Collect reported usage, with text length as a fallback for providers that omit it."""
    usage = event.get("usage") or (event.get("message") or {}).get("usage") or {}
    if usage.get("input_tokens") is not None:
        tracker["input_tokens"] = int(usage["input_tokens"] or 0)
    if usage.get("output_tokens") is not None:
        tracker["output_tokens"] = int(usage["output_tokens"] or 0)
    delta = event.get("delta") or {}
    if isinstance(delta.get("text"), str):
        tracker["output_chars"] += len(delta["text"])
    if isinstance(delta.get("thinking"), str):
        tracker["output_chars"] += len(delta["thinking"])


def _final_stream_tokens(tracker: dict) -> tuple[int, int]:
    output_tokens = tracker["output_tokens"]
    if output_tokens <= 0 and tracker["output_chars"] > 0:
        output_tokens = max(1, int(tracker["output_chars"] / 3.5))
    return tracker["input_tokens"], output_tokens


# A dead or unreachable custom-provider host used to hang for the full
# 300s before failing -- long enough that Playground just looked frozen.
# Connect fails fast (a live host completes TCP+TLS in well under this);
# read stays generous since legitimate generation can genuinely take a
# while.
async def _dispatch_custom_provider(prefix: str, payload: dict, stream: bool, display_model: str = None, anthropic_headers=None, retry_state=None):
    """
    Send an Anthropic-shaped request to an admin-added custom provider and
    return an Anthropic-shaped result, regardless of whether that provider
    speaks OpenAI or Anthropic upstream. Callers on the OpenAI-compatible
    endpoint convert their request to this shape first and the response back
    afterwards -- this function only ever deals in Anthropic shape.

    Returns either:
      ("json", status_code, dict)                          for non-streaming
      ("stream", status_code, async_generator[str] | None)  for streaming
    A None generator means the caller should fall back to the status/dict
    error path instead (used when we fail before ever reaching upstream).
    """
    info = config_module.CUSTOM_PROVIDERS.get(prefix)
    if not info:
        return "json", 400, {"error": {"message": f"Unknown provider '{prefix}'"}}

    keys = list(config_module.CUSTOM_PROVIDER_KEYS.get(prefix) or [])
    if not keys:
        return "json", 500, {"error": {"message": f"No API keys configured for provider '{prefix}'"}}
    first_key = config_module.get_current_custom_key(prefix)
    start_index = keys.index(first_key) if first_key in keys else 0
    keys = keys[start_index:] + keys[:start_index]

    base_url = info["base_url"]
    api_format = info["api_format"]
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    model = payload.get("model", "")
    # What the response should claim to be. Differs from `model` only when the
    # caller's key renames it; without this the real id leaks out through
    # message_start on the streaming path.
    shown_model = display_model or model

    last_status, last_body = 502, {"error": {"message": "All configured keys for this provider failed."}}

    for attempt, key in enumerate(keys):
        headers = config_module.custom_provider_headers(info, key, anthropic_headers)

        try:
            if api_format == "anthropic":
                url = f"{base_url}/messages"
                upstream_payload = dict(payload)

                if not stream:
                    async with _borrow_custom_client() as client:
                        resp = await client.post(url, headers=headers, json=upstream_payload)
                    if resp.status_code == 200:
                        return "json", 200, resp.json()
                    try:
                        body = resp.json()
                    except Exception:
                        body = {"error": {"message": resp.text}}
                    last_status, last_body = resp.status_code, body
                else:
                    client = _get_custom_client()
                    req = client.build_request("POST", url, headers=headers, json=upstream_payload)
                    resp = await client.send(req, stream=True)
                    if resp.status_code == 200:
                        async def _relay():
                            try:
                                async for chunk in resp.aiter_bytes():
                                    yield chunk
                            except Exception as e:
                                # The 200 headers are already on the wire, so a
                                # mid-stream failure can't become a status code
                                # any more. Emit an SSE error event instead of
                                # letting the exception tear down the connection
                                # and leave the client a bare network error.
                                yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error(str(e)))}\n\n"
                            finally:
                                await resp.aclose()
                        return "stream", 200, _relay()
                    body_bytes = await resp.aread()
                    await resp.aclose()
                    try:
                        last_body = json.loads(body_bytes)
                    except Exception:
                        last_body = {"error": {"message": body_bytes.decode(errors="replace")}}
                    last_status = resp.status_code

            else:  # openai-compatible upstream
                url = f"{base_url}/chat/completions"
                upstream_payload = build_openai_request(payload, provider=prefix)
                upstream_payload["stream"] = stream

                if not stream:
                    async with _borrow_custom_client() as client:
                        resp = await client.post(url, headers=headers, json=upstream_payload)
                    if resp.status_code == 200:
                        anthropic_resp = to_anthropic_response(resp.json(), shown_model, msg_id)
                        return "json", 200, anthropic_resp
                    try:
                        body = resp.json()
                    except Exception:
                        body = {"error": {"message": resp.text}}
                    last_status, last_body = resp.status_code, body
                else:
                    client = _get_custom_client()
                    req = client.build_request("POST", url, headers=headers, json=upstream_payload)
                    resp = await client.send(req, stream=True)
                    if resp.status_code == 200:
                        # Plenty of providers answer 200 and then put the real
                        # failure inside the stream body. stream_as_anthropic
                        # inspects the first data chunk before it emits
                        # message_start, so pulling that first event here --
                        # while we can still pick a status code and rotate to
                        # the next key -- turns an in-band error into an honest
                        # failure instead of a connection that just dies.
                        agen = stream_as_anthropic(resp, shown_model, msg_id)
                        first_event, in_band_error = None, None
                        try:
                            first_event = await agen.__anext__()
                        except StopAsyncIteration:
                            pass
                        except ValueError as e:
                            in_band_error = str(e)

                        if in_band_error is None:
                            async def _relay(agen=agen, first_event=first_event, resp=resp, client=client):
                                try:
                                    if first_event is not None:
                                        yield first_event
                                    async for chunk in agen:
                                        yield chunk
                                except Exception as e:
                                    # Same bind as above: past this point the
                                    # only way to report a failure is in-band.
                                    yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error(str(e)))}\n\n"
                                finally:
                                    await resp.aclose()
                            return "stream", 200, _relay()

                        await agen.aclose()
                        await resp.aclose()
                        last_status, last_body = 502, {"error": {"message": in_band_error}}
                    else:
                        body_bytes = await resp.aread()
                        await resp.aclose()
                        try:
                            last_body = json.loads(body_bytes)
                        except Exception:
                            last_body = {"error": {"message": body_bytes.decode(errors="replace")}}
                        last_status = resp.status_code

        except Exception as e:
            last_status, last_body = 502, {"error": {"message": f"{type(e).__name__}: {e}"}}

        if _should_retry_custom_key(last_status, last_body) and attempt < len(keys) - 1:
            config_module.rotate_custom_key(prefix)
            if retry_state is not None:
                retry_state["rotated"] = True
        else:
            break

    return "json", last_status, last_body


async def _dispatch_custom_openai(prefix: str, payload: dict, stream: bool, retry_state=None):
    """Forward an OpenAI request to an OpenAI provider without translating it."""
    info = config_module.CUSTOM_PROVIDERS[prefix]
    keys = list(config_module.CUSTOM_PROVIDER_KEYS.get(prefix) or [])
    if not keys:
        return "json", 500, {"error": {"message": f"No API keys configured for provider '{prefix}'"}}, None
    first_key = config_module.get_current_custom_key(prefix)
    start_index = keys.index(first_key) if first_key in keys else 0
    keys = keys[start_index:] + keys[:start_index]

    url = f"{info['base_url']}/chat/completions"
    last_status = 502
    last_body = {"error": {"message": "All configured keys for this provider failed."}}
    for attempt, key in enumerate(keys):
        headers = config_module.custom_provider_headers(info, key)
        try:
            if stream:
                client = _get_custom_client()
                upstream_request = client.build_request("POST", url, headers=headers, json=payload)
                response = await client.send(upstream_request, stream=True)
                if response.status_code == 200:
                    return "stream", 200, response, key
                raw_body = await response.aread()
                await response.aclose()
                try:
                    last_body = json.loads(raw_body)
                except Exception:
                    last_body = {"error": {"message": raw_body.decode(errors="replace")}}
                last_status = response.status_code
            else:
                async with _borrow_custom_client() as client:
                    response = await client.post(url, headers=headers, json=payload)
                last_status = response.status_code
                try:
                    last_body = response.json()
                except Exception:
                    last_body = {"error": {"message": response.text}}
                    if last_status == 200:
                        last_status = 502
                if last_status == 200:
                    return "json", 200, last_body, key
        except httpx.HTTPError as exc:
            last_status = 502
            last_body = {"error": {"message": f"Upstream connection failed: {type(exc).__name__}"}}

        if _should_retry_custom_key(last_status, last_body) and attempt < len(keys) - 1:
            config_module.rotate_custom_key(prefix)
            if retry_state is not None:
                retry_state["rotated"] = True
        else:
            break

    return "json", last_status, last_body, None


def _qwen_image_request(payload: dict) -> dict:
    prompt = ""
    image_urls = []

    for message in reversed(payload.get("messages", [])):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            prompt = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("text"):
                    text_parts.append(block["text"])
                elif block.get("type") == "text" and block.get("text"):
                    text_parts.append(block["text"])
                elif block.get("type") == "image" and isinstance(block.get("source"), dict):
                    source = block["source"]
                    if source.get("type") == "url" and source.get("url"):
                        image_urls.append(source["url"])
                    elif source.get("type") == "base64" and source.get("data"):
                        media_type = source.get("media_type", "image/png")
                        image_urls.append(f"data:{media_type};base64,{source['data']}")
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url")
                    if url:
                        image_urls.append(url)
                elif block.get("image"):
                    image_urls.append(block["image"])
            prompt = "\n".join(text_parts)
        break

    content = [{"image": url} for url in image_urls]
    content.append({"text": prompt or ("Edit this image" if image_urls else "Generate an image")})

    return {
        "model": payload["model"],
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ]
        },
        "parameters": {
            "prompt_extend": True,
            "watermark": False,
            "size": "1024*1024",
            "n": 1,
        },
    }


def _qwen_image_response(data: dict, model: str, msg_id: str) -> dict:
    choices = data.get("output", {}).get("choices", [])
    blocks = choices[0].get("message", {}).get("content", []) if choices else []
    content = [
        {"type": "image", "source": {"type": "url", "url": block["image"]}}
        for block in blocks
        if isinstance(block, dict) and block.get("image")
    ]
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
        },
    }


@router.get("/v1/models")
@router.get("/models")
@router.get("/v1/v1/models")
async def list_models(request: Request):
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid router password."}})

    models = []
    disabled = config_module.DISABLED_PROVIDERS
    if "bm" not in disabled:
        for m in config_module.BLUESMINDS_MODELS:
            models.append(f"bm/{m}")
    if "nry" not in disabled:
        for m in config_module.NARA_MODELS:
            models.append(f"nry/{m}")
    if "dahl" not in disabled:
        for m in config_module.DAHL_MODELS:
            models.append(f"dh/{m}")
    if "qc" not in disabled:
        for m in config_module.QWEN_CLOUD_MODELS:
            models.append(f"qc/{m}")
    if "marketku" not in disabled:
        for m in config_module.MARKETKU_MODELS:
            models.append(f"mk/{m}")
    for prefix, info in config_module.CUSTOM_PROVIDERS.items():
        for m in info.get("models") or []:
            models.append(f"{prefix}/{m}")

    # A key with a model allowlist should only see what it can actually call,
    # and aliased models are advertised under their new name.
    key_row = _router_key(request)
    if key_row:
        allowed_raw = (key_row.get("allowed_models") or "").strip()
        if allowed_raw:
            allowed = {m.strip() for m in allowed_raw.split(",") if m.strip()}
            models = [m for m in models if m in allowed]
        aliases = _key_aliases(request)
        if aliases:
            models = [aliases.get(m, m) for m in models]

    data = []
    for m in models:
        data.append({
            "id": m,
            "object": "model",
            "created": 1700000000,
            "owned_by": "iyan-router"
        })

    return JSONResponse(content={"object": "list", "data": data})


@router.post("/v1/messages/count_tokens")
@router.post("/v1/v1/messages/count_tokens")
async def count_tokens(request: Request):
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key."}})
    body, error = await _read_json_payload(request)
    if error:
        return error
    tokens = estimate_tokens(body)
    return {"input_tokens": tokens}


@router.post("/v1/messages")
@router.post("/v1/v1/messages")
async def messages(request: Request):
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid router password."}})

    payload, error = await _read_json_payload(request)
    if error:
        return error
    if not isinstance(payload.get("model"), str) or not payload["model"].strip():
        return JSONResponse(status_code=400, content={"error": {"message": "A non-empty model is required."}})
    if not isinstance(payload.get("messages"), list):
        return JSONResponse(status_code=400, content={"error": {"message": "messages must be an array."}})

    # Custom (admin-added) providers get a self-contained dispatch path,
    # short-circuiting before built-in provider routing below.
    # An aliased name has to become the real model before anything routes on
    # it; display_model is what the response will claim to be.
    requested_model_raw, display_model = _resolve_alias(request, payload.get("model", "") or "")
    payload["model"] = requested_model_raw

    denied = _model_allowed_for_key(request, requested_model_raw)
    if denied:
        return JSONResponse(status_code=403, content={"error": {"message": denied}})

    # Applied here, before provider dispatch, so it reaches built-in and
    # custom providers alike.
    _inject_anthropic_system(payload, _key_model_prompt(request, requested_model_raw))

    for cprefix in config_module.CUSTOM_PROVIDERS:
        if requested_model_raw.startswith(f"{cprefix}/"):
            payload["model"] = requested_model_raw[len(cprefix) + 1:]
            want_stream = bool(payload.get("stream"))
            start_req_time = time.time()
            retry_state = {}
            kind, status, body = await _dispatch_custom_provider(
                cprefix, payload, want_stream, display_model, request.headers,
                retry_state=retry_state,
            )
            log_model = f"{cprefix}/{payload['model']}"
            # Echo the alias back rather than the real model id.
            if kind == "json" and isinstance(body, dict) and body.get("model"):
                body["model"] = display_model
            elapsed_ms = int((time.time() - start_req_time) * 1000)
            if kind == "stream":
                async def _wrapped():
                    tracker = {
                        "input_tokens": estimate_tokens(payload),
                        "output_tokens": 0,
                        "output_chars": 0,
                    }
                    buffer = ""
                    try:
                        async for chunk in body:
                            text = chunk if isinstance(chunk, str) else chunk.decode(errors="ignore")
                            buffer += text
                            lines = buffer.split("\n")
                            buffer = lines.pop()
                            for line in lines:
                                if not line.startswith("data: "):
                                    continue
                                try:
                                    _update_anthropic_stream_usage(json.loads(line[6:]), tracker)
                                except Exception:
                                    pass
                            yield chunk
                    finally:
                        input_tokens, output_tokens = _final_stream_tokens(tracker)
                        add_request_log(
                            log_model, status, "custom", retry_state.get("rotated", False),
                            int((time.time() - start_req_time) * 1000),
                            input_tokens, output_tokens, provider=cprefix,
                        )
                        await _bill_router_key(request, input_tokens + output_tokens)
                        await _broadcast_request_log()
                return StreamingResponse(_wrapped(), media_type="text/event-stream")
            usage = (body or {}).get("usage") or {}
            input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0
            add_request_log(
                log_model, status, "custom", retry_state.get("rotated", False), elapsed_ms,
                input_tokens, output_tokens,
                provider=cprefix,
            )
            await _bill_router_key(request, input_tokens + output_tokens)
            await _broadcast_request_log()
            return JSONResponse(status_code=status, content=body)

    provider = _builtin_provider_for_model(requested_model_raw)
    if provider in config_module.DISABLED_PROVIDERS:
        return JSONResponse(status_code=503, content={"error": {"message": f"Provider '{provider}' has been removed."}})

    provider_prefixes = {
        "bm": "bm/", "nry": "nry/", "dahl": "dh/",
        "qc": "qc/", "marketku": "mk/",
    }
    prefix = provider_prefixes.get(provider)
    upstream_model = requested_model_raw[len(prefix):] if prefix and requested_model_raw.startswith(prefix) else requested_model_raw
    if provider == "dahl":
        upstream_model = resolve_dahl_model(upstream_model)
    payload["model"] = upstream_model

    if provider == "bm":
        current_key = get_current_bm_key() if BM_API_KEYS else BLUESMINDS_API_KEY or ""
    elif provider == "nry":
        current_key = get_current_nr_key() if NR_API_KEYS else ""
    elif provider == "dahl":
        current_key = get_current_dahl_key() if DAHL_API_KEYS else ""
    elif provider == "qc":
        current_key = get_current_qc_key_for_model(payload.get("model", "")) if QC_API_KEYS else ""
    elif provider == "marketku":
        current_key = get_current_marketku_key() if MARKETKU_API_KEYS else ""


    if provider == "bm":
        upstream_base_url = BLUESMINDS_BASE_URL
        log_model = f"bm/{payload['model']}"
    elif provider == "nry":
        upstream_base_url = NARA_BASE_URL
        log_model = f"nry/{payload['model']}"
    elif provider == "dahl":
        upstream_base_url = DAHL_BASE_URL
        log_model = f"dh/{payload['model'].split('/', 1)[-1]}"
    elif provider == "qc":
        upstream_base_url = QWEN_CLOUD_BASE_URL
        log_model = f"qc/{payload['model']}"
    elif provider == "marketku":
        upstream_base_url = MARKETKU_BASE_URL
        log_model = f"mk/{payload['model']}"
    else:
        upstream_base_url = BLUESMINDS_BASE_URL
        log_model = f"bm/{payload['model']}"

    # What the response claims to be. Logs and routing stats keep using
    # log_model so the dashboard still shows the model that actually ran.
    display_log_model = display_model if display_model != requested_model_raw else log_model

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    upstream_req = build_openai_request(payload, provider=provider)
    upstream_endpoint = f"{upstream_base_url}/chat/completions"

    input_tokens = estimate_tokens(payload)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "kimchi/0.2.0",
    }

    original_messages = upstream_req["messages"][:]
    compact_levels = [None, 20, 6]

    if provider == "bm":
        api_keys_to_use = BM_API_KEYS
    elif provider == "nry":
        api_keys_to_use = NR_API_KEYS
    elif provider == "dahl":
        api_keys_to_use = DAHL_API_KEYS
    elif provider == "qc":
        api_keys_to_use = QC_API_KEYS
    elif provider == "marketku":
        api_keys_to_use = MARKETKU_API_KEYS
    else:
        api_keys_to_use = BM_API_KEYS

    if not api_keys_to_use:
        if provider == "bm" and BLUESMINDS_API_KEY:
            api_keys_to_use = [BLUESMINDS_API_KEY]
        else:
            return JSONResponse(status_code=500, content={"error": "No upstream API keys available"})

    requested_qc_model = payload.get("model") if provider == "qc" else None
    is_qwen_image = provider == "qc" and "image" in payload.get("model", "").lower()

    if is_qwen_image:
        image_endpoint = "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        image_request = _qwen_image_request(payload)
        last_status = 500
        last_error = {"error": {"message": "Image generation failed"}}

        for _ in range(len(QC_API_KEYS)):
            current_key = get_current_qc_key_for_model(requested_qc_model)
            if not current_key:
                return JSONResponse(
                    status_code=429,
                    content={"error": {"message": f"All QC keys are exhausted for model '{requested_qc_model}'."}},
                )
            start_req_time = time.time()
            async with _borrow_upstream_client() as client:
                resp = await client.post(
                    image_endpoint,
                    headers={
                        "Authorization": f"Bearer {current_key}",
                        "Content-Type": "application/json",
                    },
                    json=image_request,
                )

            try:
                data = resp.json()
            except Exception:
                data = {"error": {"message": resp.text or f"HTTP {resp.status_code}"}}

            if resp.status_code == 200:
                result = _qwen_image_response(data, display_log_model, msg_id)
                total_ms = int((time.time() - start_req_time) * 1000)
                add_request_log(log_model, 200, current_key, False, total_ms, input_tokens, 0)
                await _bill_router_key(request, input_tokens)
                await sse_broadcaster.broadcast("log", recent_requests[0] if recent_requests else {})
                await sse_broadcaster.broadcast("status", await _build_status_dict(include_details=False))
                return JSONResponse(result)

            last_status = resp.status_code
            message = data.get("message") or data.get("error", {}).get("message") or f"HTTP {resp.status_code}"
            last_error = {"error": {"message": message}}
            add_request_log(
                log_model,
                resp.status_code,
                current_key,
                True,
                int((time.time() - start_req_time) * 1000),
            )
            if resp.status_code in _RETRYABLE_UPSTREAM_STATUSES and _rotate_qc_after_failure(
                requested_qc_model, current_key, resp.status_code, data
            ):
                continue
            break

        return JSONResponse(status_code=last_status, content=last_error)

    if upstream_req.get("stream"):
        async def generate():
            nonlocal requested_qc_model
            nonlocal log_model
            last_error_status = 429
            last_error_content = {"error": {"message": "All configured API keys are rate limited or unauthorized."}}

            for c_idx, compact_level in enumerate(compact_levels):
                if compact_level is not None:
                    upstream_req["messages"] = compact_messages(original_messages, keep_last=compact_level)
                    print(f"[LOG] Auto-compacting context → keeping last {compact_level} messages ({len(upstream_req['messages'])} total)")

                context_window_hit = False
                rotated_occurred = False

                for attempt in range(len(api_keys_to_use)):
                    if provider == "bm":
                        current_key = get_current_bm_key()
                    elif provider == "nry":
                        current_key = get_current_nr_key()
                    elif provider == "dahl":
                        current_key = get_current_dahl_key()
                    elif provider == "qc":
                        current_key = get_current_qc_key_for_model(requested_qc_model)
                    elif provider == "marketku":
                        current_key = get_current_marketku_key()
                    else:
                        current_key = get_current_bm_key()

                    if provider == "qc" and not current_key:
                        exhausted_error = f"All QC keys are exhausted for model '{requested_qc_model}'."
                        yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error(exhausted_error))}\n\n"
                        return

                    headers["Authorization"] = f"Bearer {current_key}"

                    start_req_time = time.time()
                    async with _borrow_upstream_client() as client:
                        try:
                            has_yielded = False
                            first_token_time = None
                            async with client.stream(
                                "POST",
                                upstream_endpoint,
                                headers=headers,
                                json=upstream_req,
                            ) as resp:
                                if resp.status_code in (401, 402, 403, 404, 429, 500, 502, 503, 504):
                                    err_data = None
                                    try:
                                        await resp.aread()
                                        err_data = resp.json()
                                    except Exception:
                                        err_data = {"error": f"HTTP {resp.status_code} error body not readable"}

                                    if err_data and is_context_window_error(err_data):
                                        if c_idx < len(compact_levels) - 1:
                                            print(f"[LOG] Context window exceeded (status {resp.status_code}), auto-compacting...")
                                            context_window_hit = True
                                            break
                                        else:
                                            print(f"[LOG] Context window exceeded after all compactions (status {resp.status_code}). Returning 400 without key rotation.")
                                            add_request_log(log_model, 400, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                                            yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error(err_data))}\n\n"
                                            return

                                    rotated_occurred = True
                                    add_request_log(log_model, resp.status_code, current_key, True, int((time.time() - start_req_time) * 1000))

                                    if provider == "qc" and requested_qc_model:
                                        if _rotate_qc_after_failure(
                                            requested_qc_model, current_key, resp.status_code, err_data
                                        ):
                                            print(f"[LOG] QC model {requested_qc_model} failed on key, trying next key for same model")
                                            continue

                                    if provider == "bm":
                                        rotate_bm_key()
                                    elif provider == "nry":
                                        rotate_nr_key()
                                    elif provider == "dahl":
                                        rotate_dahl_key()
                                    elif provider == "qc":
                                        pass
                                    elif provider == "marketku":
                                        rotate_marketku_key()
                                    last_error_status = resp.status_code
                                    last_error_content = err_data or {"error": f"HTTP {resp.status_code} error"}
                                    await sse_broadcaster.broadcast("status", await _build_status_dict())
                                    continue

                                if resp.status_code != 200:
                                    try:
                                        await resp.aread()
                                        err_data = resp.json()
                                    except Exception:
                                        err_data = {"error": f"HTTP {resp.status_code} error body not readable"}
                                    if resp.status_code == 400 and is_context_window_error(err_data) and c_idx < len(compact_levels) - 1:
                                        print(f"[LOG] Context window exceeded, auto-compacting...")
                                        context_window_hit = True
                                        break
                                    add_request_log(log_model, resp.status_code, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                                    yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error(err_data))}\n\n"
                                    return

                                token_tracker = {"output_tokens": 0}
                                async for chunk in stream_as_anthropic(resp, display_log_model, msg_id, input_tokens, token_tracker):
                                    has_yielded = True
                                    if first_token_time is None:
                                        first_token_time = time.time()
                                    yield chunk
                                total_ms = int((time.time() - start_req_time) * 1000)
                                ttft_ms = int((first_token_time - start_req_time) * 1000) if first_token_time else total_ms
                                add_request_log(log_model, 200, current_key, rotated_occurred, total_ms, input_tokens, token_tracker["output_tokens"])
                                await _bill_router_key(request, input_tokens + token_tracker["output_tokens"])

                                threshold = config_module.SLOW_RESPONSE_THRESHOLD_MS
                                if threshold > 0 and ttft_ms > threshold and len(api_keys_to_use) > 1:
                                    print(f"[LOG] Slow TTFT {ttft_ms}ms > {threshold}ms, rotating {provider} key proactively")
                                    if provider == "bm":
                                        rotate_bm_key(reason="Slow")
                                    elif provider == "nry":
                                        rotate_nr_key(reason="Slow")
                                    elif provider == "marketku":
                                        rotate_marketku_key()
                                await sse_broadcaster.broadcast("log", recent_requests[0] if recent_requests else {})
                                await sse_broadcaster.broadcast("status", await _build_status_dict(include_details=rotated_occurred))
                                return
                        except Exception as e:
                            print(f"[STREAM ERROR] Exception during attempt {attempt} (key: {current_key[:10]}...): {type(e).__name__}: {str(e)}")
                            import traceback; traceback.print_exc()

                            if is_context_window_error(str(e)):
                                if c_idx < len(compact_levels) - 1:
                                    print(f"[LOG] Context window exceeded (parsed from stream exception), triggering auto-compacting...")
                                    context_window_hit = True
                                    break
                                else:
                                    print(f"[LOG] Context window exceeded after all compactions. Returning 400 without key rotation.")
                                    add_request_log(log_model, 400, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                                    if not has_yielded:
                                        yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error('Konteks terlalu panjang bahkan setelah auto-compact. Silakan mulai percakapan baru.'))}\n\n"
                                    return

                            if has_yielded or attempt == len(api_keys_to_use) - 1:
                                add_request_log(log_model, 500, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                                if not has_yielded:
                                    yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error(str(e)))}\n\n"
                                return
                            rotated_occurred = True
                            add_request_log(log_model, 500, current_key, True, int((time.time() - start_req_time) * 1000))
                            if provider == "bm":
                                rotate_bm_key()
                            elif provider == "nry":
                                rotate_nr_key()
                            elif provider == "dahl":
                                rotate_dahl_key()
                            elif provider == "qc":
                                rotate_qc_key_for_model(requested_qc_model, after_key=current_key)
                            elif provider == "marketku":
                                rotate_marketku_key()
                            last_error_status = 500
                            last_error_content = {"error": str(e)}
                            await sse_broadcaster.broadcast("status", await _build_status_dict())

                if context_window_hit:
                    continue

                yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error(last_error_content))}\n\n"
                return

            yield f"event: error\ndata: {json.dumps(to_anthropic_stream_error('Konteks terlalu panjang bahkan setelah auto-compact. Silakan mulai percakapan baru.'))}\n\n"
            return

        return StreamingResponse(generate(), media_type="text/event-stream")

    last_error_status = 429
    last_error_content = {"error": {"message": "All configured API keys are rate limited or unauthorized."}}

    for c_idx, compact_level in enumerate(compact_levels):
        if compact_level is not None:
            upstream_req["messages"] = compact_messages(original_messages, keep_last=compact_level)
            print(f"[LOG] Auto-compacting context → keeping last {compact_level} messages ({len(upstream_req['messages'])} total)")

        context_window_hit = False
        rotated_occurred = False

        for attempt in range(len(api_keys_to_use)):
            if provider == "bm":
                current_key = get_current_bm_key()
            elif provider == "nry":
                current_key = get_current_nr_key()
            elif provider == "dahl":
                current_key = get_current_dahl_key()
            elif provider == "qc":
                current_key = get_current_qc_key_for_model(requested_qc_model)
            elif provider == "marketku":
                current_key = get_current_marketku_key()
            else:
                current_key = get_current_bm_key()

            if provider == "qc" and not current_key:
                return JSONResponse(
                    status_code=429,
                    content={"error": {"message": f"All QC keys are exhausted for model '{requested_qc_model}'."}},
                )

            headers["Authorization"] = f"Bearer {current_key}"
            for h in ("x-api-key", "anthropic-version"):
                headers.pop(h, None)

            start_req_time = time.time()
            try:
                async with _borrow_upstream_client() as client:
                    resp = await client.post(
                        upstream_endpoint,
                        headers=headers,
                        json=upstream_req,
                    )
                if resp.status_code in (401, 402, 403, 404, 429, 500, 502, 503, 504):
                    err_json = None
                    try:
                        err_json = resp.json()
                    except Exception:
                        err_json = {"error": resp.text}

                    if err_json and is_context_window_error(err_json):
                        if c_idx < len(compact_levels) - 1:
                            print(f"[LOG] Context window exceeded (status {resp.status_code} non-stream), auto-compacting...")
                            context_window_hit = True
                            break
                        else:
                            print(f"[LOG] Context window exceeded after all compactions (status {resp.status_code} non-stream). Returning 400 without key rotation.")
                            add_request_log(log_model, 400, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                            return JSONResponse(status_code=400, content=err_json)

                    rotated_occurred = True
                    add_request_log(log_model, resp.status_code, current_key, True, int((time.time() - start_req_time) * 1000))

                    if provider == "qc" and requested_qc_model:
                        if _rotate_qc_after_failure(
                            requested_qc_model, current_key, resp.status_code, err_json
                        ):
                            print(f"[LOG] QC model {requested_qc_model} failed on key, trying next key for same model")
                            continue

                    if provider == "bm":
                        rotate_bm_key()
                    elif provider == "nry":
                        rotate_nr_key()
                    elif provider == "dahl":
                        rotate_dahl_key()
                    elif provider == "qc":
                        pass
                    elif provider == "marketku":
                        rotate_marketku_key()
                    last_error_status = resp.status_code
                    last_error_content = err_json or {"error": resp.text}
                    await sse_broadcaster.broadcast("status", await _build_status_dict())
                    continue
                if resp.status_code != 200:
                    try:
                        err_json = resp.json()
                    except Exception:
                        err_json = {"error": resp.text}
                    if resp.status_code == 400 and is_context_window_error(err_json) and c_idx < len(compact_levels) - 1:
                        print(f"[LOG] Context window exceeded, auto-compacting...")
                        context_window_hit = True
                        break
                    add_request_log(log_model, resp.status_code, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                    return JSONResponse(status_code=resp.status_code, content=err_json)
                openai_resp = resp.json()
                anthropic_resp = to_anthropic_response(openai_resp, display_log_model, msg_id)
                usage = anthropic_resp.get("usage", {})
                output_tokens = usage.get("output_tokens", 0)
                cached_tokens = _extract_cached_tokens(openai_resp)
                total_ms = int((time.time() - start_req_time) * 1000)
                add_request_log(log_model, 200, current_key, rotated_occurred, total_ms, input_tokens, output_tokens, cached_tokens)
                await _bill_router_key(request, input_tokens + output_tokens)

                threshold = config_module.SLOW_RESPONSE_THRESHOLD_MS
                if threshold > 0 and total_ms > threshold and len(api_keys_to_use) > 1:
                    print(f"[LOG] Slow response {total_ms}ms > {threshold}ms, rotating {provider} key proactively")
                    if provider == "bm":
                        rotate_bm_key(reason="Slow")
                    elif provider == "nry":
                        rotate_nr_key(reason="Slow")
                    elif provider == "marketku":
                        rotate_marketku_key()
                    # Dahl upstream is inherently slow; don't rotate on slow total time
                await sse_broadcaster.broadcast("log", recent_requests[0] if recent_requests else {})
                await sse_broadcaster.broadcast("status", await _build_status_dict(include_details=rotated_occurred))
                return JSONResponse(anthropic_resp)
            except Exception as e:
                print(f"[LOG] Request attempt {attempt} with key {current_key[:10]}... failed: {type(e).__name__}: {str(e)}")

                if is_context_window_error(str(e)):
                    if c_idx < len(compact_levels) - 1:
                        print(f"[LOG] Context window exceeded (parsed from non-stream exception), triggering auto-compacting...")
                        context_window_hit = True
                        break
                    else:
                        print(f"[LOG] Context window exceeded after all compactions (non-stream). Returning 400 without key rotation.")
                        add_request_log(log_model, 400, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                        return JSONResponse(
                            status_code=400,
                            content={"error": {"message": "Konteks terlalu panjang bahkan setelah auto-compact. Silakan mulai percakapan baru."}}
                        )

                if attempt == len(api_keys_to_use) - 1:
                    import traceback
                    traceback.print_exc()
                    add_request_log(log_model, 500, current_key, rotated_occurred, int((time.time() - start_req_time) * 1000))
                    return JSONResponse(status_code=500, content={"error": str(e)})
                rotated_occurred = True
                add_request_log(log_model, 500, current_key, True, int((time.time() - start_req_time) * 1000))
                if provider == "bm":
                    rotate_bm_key()
                elif provider == "nry":
                    rotate_nr_key()
                elif provider == "dahl":
                    rotate_dahl_key()
                elif provider == "qc":
                    rotate_qc_key_for_model(requested_qc_model, after_key=current_key)
                elif provider == "marketku":
                    rotate_marketku_key()
                last_error_status = 500
                last_error_content = {"error": str(e)}
                await sse_broadcaster.broadcast("status", await _build_status_dict())

        if context_window_hit:
            continue

        return JSONResponse(status_code=last_error_status, content=last_error_content)

    return JSONResponse(
        status_code=400,
        content={"error": {"message": "Konteks terlalu panjang bahkan setelah auto-compact. Silakan mulai percakapan baru."}}
    )


def _aggregate_openai_sse(raw_text: str, fallback_model: str):
    """
    Reconstruct a normal chat.completion object from an SSE response body.

    Some upstreams ignore `stream: false` under certain conditions (large
    prompts, particular models) and stream anyway. resp.json() then fails,
    and without this the raw SSE text used to get dumped verbatim into an
    "error" field on an otherwise-200 response. Returns None if the text
    doesn't look like SSE data at all, so the caller can fall back to
    reporting a real error.
    """
    content_parts = []
    tool_calls = {}
    finish_reason = "stop"
    model_name = None
    usage = None
    saw_data = False

    for line in raw_text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            continue
        try:
            chunk = json.loads(data_str)
        except Exception:
            continue
        saw_data = True
        model_name = chunk.get("model") or model_name
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(idx, {"id": tc.get("id"), "type": "function", "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    if not saw_data:
        return None

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        finish_reason = "tool_calls"

    return {
        "id": f"chatcmpl-reconstructed-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name or fallback_model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage or {},
    }


async def _relay_openai_upstream_stream(
    resp: httpx.Response,
    *,
    requested_model: str,
    display_model: str,
    current_key: str,
    provider: str,
    started_at: float,
    request: Request,
    strip_thinking: bool = True,
    input_tokens_estimate: int = 0,
    rotated: bool = False,
):
    """Relay an already-open upstream SSE response and account for usage."""
    import re

    should_strip = strip_thinking and requested_model.endswith(("-thinking", "-agentic", "-thinking-agentic"))
    buffer = ""
    inside_thinking = False
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    output_chars = 0

    try:
        async for chunk in resp.aiter_bytes():
            buffer += chunk.decode("utf-8", errors="ignore")
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                if not line.startswith("data: "):
                    yield f"{line}\n".encode("utf-8")
                    continue

                try:
                    data = json.loads(line[6:])
                except Exception:
                    yield f"{line}\n".encode("utf-8")
                    continue

                usage = data.get("usage") or {}
                if usage.get("prompt_tokens"):
                    token_usage["prompt_tokens"] = usage["prompt_tokens"]
                if usage.get("completion_tokens"):
                    token_usage["completion_tokens"] = usage["completion_tokens"]

                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    output_chars += len(delta.get("content") or "")
                    for tool_call in delta.get("tool_calls") or []:
                        function = tool_call.get("function") or {}
                        output_chars += len(function.get("name") or "") + len(function.get("arguments") or "")

                if should_strip and (data.get("choices") or []):
                    delta = data["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        original = content
                        if "<thinking>" in content:
                            inside_thinking = True
                        if "</thinking>" in content:
                            inside_thinking = False
                            content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
                        elif inside_thinking or "<thinking>" in content:
                            content = re.sub(r"<thinking>.*", "", content, flags=re.DOTALL)
                        if original != content:
                            logger.info("[STREAM] Stripped: %r -> %r", original, content)
                        data["choices"][0]["delta"]["content"] = content
                        if not content:
                            continue

                if data.get("model"):
                    data["model"] = display_model
                yield f"data: {json.dumps(data)}\n".encode("utf-8")

        if buffer:
            yield buffer.encode("utf-8")
    finally:
        await resp.aclose()
        if input_tokens_estimate and not token_usage["prompt_tokens"]:
            token_usage["prompt_tokens"] = input_tokens_estimate
        if output_chars and not token_usage["completion_tokens"]:
            token_usage["completion_tokens"] = max(1, int(output_chars / 3.5))
        total_ms = int((time.time() - started_at) * 1000)
        add_request_log(
            requested_model,
            200,
            current_key,
            rotated,
            total_ms,
            token_usage["prompt_tokens"],
            token_usage["completion_tokens"],
            provider=provider,
        )
        await _bill_router_key(
            request, token_usage["prompt_tokens"] + token_usage["completion_tokens"]
        )
        await _broadcast_request_log()


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible endpoint for OpenChamber and other OpenAI-compatible clients.
    Keeps the external API OpenAI-shaped while using the same provider prefixes as
    /v1/messages: kc, cv, bm, nry, dh, qc, mk, at, wz.

    Registered at both /v1/chat/completions and /chat/completions -- an
    OpenAI SDK configured with this router's *Anthropic* base URL (no /v1)
    would otherwise call the bare path and get a 404, since OpenAI clients
    append "/chat/completions" directly to whatever base_url they're given
    rather than always assuming a /v1 prefix.
    """
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key"}})

    try:
        openai_payload, error = await _read_json_payload(request)
        if error:
            return error
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid request."}})

    payload = dict(openai_payload)
    requested_model, display_model = _resolve_alias(request, payload.get("model") or "bm/claude-3-5-sonnet-20241022")
    payload["model"] = requested_model
    if not isinstance(payload.get("messages"), list):
        return JSONResponse(status_code=400, content={"error": {"message": "messages must be an array."}})
    print(f"[CHAT-COMPLETIONS] Model: {requested_model}, stream: {payload.get('stream')}", flush=True)

    denied = _model_allowed_for_key(request, requested_model)
    if denied:
        return JSONResponse(status_code=403, content={"error": {"message": denied}})

    _inject_openai_system(payload, _key_model_prompt(request, requested_model))

    for cprefix in config_module.CUSTOM_PROVIDERS:
        if requested_model.startswith(f"{cprefix}/"):
            model_name = requested_model[len(cprefix) + 1:]
            if config_module.CUSTOM_PROVIDERS[cprefix]["api_format"] == "openai":
                upstream_payload = {**payload, "model": model_name}
                start_req_time = time.time()
                retry_state = {}
                kind, status, body, current_key = await _dispatch_custom_openai(
                    cprefix, upstream_payload, bool(payload.get("stream")), retry_state=retry_state,
                )
                log_model = f"{cprefix}/{model_name}"
                if kind == "stream":
                    return StreamingResponse(
                        _relay_openai_upstream_stream(
                            body,
                            requested_model=log_model,
                            display_model=display_model,
                            current_key=current_key,
                            provider=cprefix,
                            started_at=start_req_time,
                            request=request,
                            strip_thinking=False,
                            input_tokens_estimate=estimate_tokens(upstream_payload),
                            rotated=retry_state.get("rotated", False),
                        ),
                        media_type="text/event-stream",
                    )
                usage = body.get("usage") or {} if isinstance(body, dict) else {}
                input_tokens = usage.get("prompt_tokens", 0) or 0
                output_tokens = usage.get("completion_tokens", 0) or 0
                add_request_log(
                    log_model, status, "custom", retry_state.get("rotated", False),
                    int((time.time() - start_req_time) * 1000),
                    input_tokens, output_tokens, provider=cprefix,
                )
                if status == 200:
                    await _bill_router_key(request, input_tokens + output_tokens)
                    if isinstance(body, dict) and body.get("model"):
                        body["model"] = display_model
                await _broadcast_request_log()
                return JSONResponse(status_code=status, content=body)

            system_prompt, anthropic_messages = openai_to_anthropic_messages(payload.get("messages") or [])
            if "max_tokens" not in payload:
                return JSONResponse(status_code=400, content={"error": {"message": "max_tokens is required for Anthropic-format providers."}})
            anthropic_payload = {
                "model": model_name,
                "messages": anthropic_messages,
                "max_tokens": payload["max_tokens"],
            }
            if "temperature" in payload:
                anthropic_payload["temperature"] = payload["temperature"]
            if "top_p" in payload:
                anthropic_payload["top_p"] = payload["top_p"]
            if "stop" in payload:
                anthropic_payload["stop_sequences"] = payload["stop"]
            if system_prompt:
                anthropic_payload["system"] = system_prompt
            # Without these, an agentic client calling through the OpenAI-
            # compatible endpoint against a custom provider never got its
            # tool schema forwarded at all -- the upstream model had no
            # idea what functions existed, so it could only narrate intent
            # in prose instead of emitting a real tool call.
            tools = openai_tools_to_anthropic(payload.get("tools"))
            if tools:
                anthropic_payload["tools"] = tools
            tool_choice = openai_tool_choice_to_anthropic(payload.get("tool_choice"))
            if tool_choice:
                anthropic_payload["tool_choice"] = tool_choice
            if payload.get("parallel_tool_calls") is False and anthropic_payload.get("tool_choice", {}).get("type") != "none":
                anthropic_payload.setdefault("tool_choice", {"type": "auto"})["disable_parallel_tool_use"] = True
            want_stream = bool(payload.get("stream"))
            start_req_time = time.time()
            retry_state = {}
            kind, status, body = await _dispatch_custom_provider(
                cprefix, anthropic_payload, want_stream, display_model, request.headers,
                retry_state=retry_state,
            )
            log_model = f"{cprefix}/{model_name}"
            usage = (body or {}).get("usage") or {} if kind == "json" else {}
            if kind == "json":
                add_request_log(
                    log_model, status, "custom", retry_state.get("rotated", False), int((time.time() - start_req_time) * 1000),
                    usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0,
                    provider=cprefix,
                )
                await _bill_router_key(request, (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0))
                await _broadcast_request_log()

            if status != 200:
                return JSONResponse(status_code=status, content=body)
            if kind == "json":
                return JSONResponse(content=anthropic_to_openai_response(body, display_model))

            async def _relay_openai_stream():
                # `body` yields either whole SSE-event strings (upstream was
                # openai-format, already reassembled by stream_as_anthropic)
                # or raw, possibly-partial byte chunks (upstream was
                # anthropic-format, relayed as-is) -- buffer defensively so
                # a line split across two chunks doesn't get silently dropped.
                convert = make_anthropic_to_openai_stream_converter(display_model)
                buffer = ""
                tracker = {
                    "input_tokens": estimate_tokens(anthropic_payload),
                    "output_tokens": 0,
                    "output_chars": 0,
                }
                try:
                    async for chunk in body:
                        text = chunk if isinstance(chunk, str) else chunk.decode(errors="ignore")
                        buffer += text
                        lines = buffer.split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            line = line.strip()
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:].strip()
                            if data_str in ("[DONE]", ""):
                                continue
                            try:
                                chunk_data = json.loads(data_str)
                            except Exception:
                                continue
                            _update_anthropic_stream_usage(chunk_data, tracker)
                            out = convert(chunk_data)
                            if out:
                                yield out.encode()
                    yield b"data: [DONE]\n\n"
                finally:
                    input_tokens, output_tokens = _final_stream_tokens(tracker)
                    add_request_log(
                        log_model, status, "custom", retry_state.get("rotated", False),
                        int((time.time() - start_req_time) * 1000),
                        input_tokens, output_tokens, provider=cprefix,
                    )
                    await _bill_router_key(request, input_tokens + output_tokens)
                    await _broadcast_request_log()

            return StreamingResponse(_relay_openai_stream(), media_type="text/event-stream")

    provider = _builtin_provider_for_model(requested_model)

    if provider in config_module.DISABLED_PROVIDERS:
        return JSONResponse(status_code=503, content={"error": {"message": f"Provider '{provider}' has been removed."}})

    provider_prefixes = {
        "bm": "bm/",
        "nry": "nry/",
        "dahl": "dh/",
        "qc": "qc/",
        "marketku": "mk/",
    }
    prefix = provider_prefixes.get(provider)
    upstream_model = requested_model[len(prefix):] if prefix and requested_model.startswith(prefix) else requested_model
    if provider == "dahl":
        upstream_model = resolve_dahl_model(upstream_model)
    payload["model"] = upstream_model

    if provider == "bm":
        upstream_base_url = BLUESMINDS_BASE_URL
        api_keys_to_use = BM_API_KEYS or ([BLUESMINDS_API_KEY] if BLUESMINDS_API_KEY else [])
        get_key = get_current_bm_key
        rotate = rotate_bm_key
    elif provider == "nry":
        upstream_base_url = NARA_BASE_URL
        api_keys_to_use = NR_API_KEYS
        get_key = get_current_nr_key
        rotate = rotate_nr_key
    elif provider == "dahl":
        upstream_base_url = DAHL_BASE_URL
        api_keys_to_use = DAHL_API_KEYS
        get_key = get_current_dahl_key
        rotate = rotate_dahl_key
    elif provider == "qc":
        upstream_base_url = QWEN_CLOUD_BASE_URL
        api_keys_to_use = QC_API_KEYS
        get_key = lambda: get_current_qc_key_for_model(upstream_model)
        rotate = None
    elif provider == "marketku":
        upstream_base_url = MARKETKU_BASE_URL
        api_keys_to_use = MARKETKU_API_KEYS
        get_key = get_current_marketku_key
        rotate = rotate_marketku_key
    else:
        upstream_base_url = BLUESMINDS_BASE_URL
        api_keys_to_use = BM_API_KEYS or ([BLUESMINDS_API_KEY] if BLUESMINDS_API_KEY else [])
        get_key = get_current_bm_key
        rotate = rotate_bm_key

    if not api_keys_to_use:
        return JSONResponse(status_code=500, content={"error": {"message": "No upstream API keys available"}})

    upstream_endpoint = f"{upstream_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "iyan-router/openai-compatible",
    }

    last_status = 429
    last_content = {"error": {"message": "All configured API keys are rate limited or unauthorized."}}

    for attempt in range(len(api_keys_to_use)):
        current_key = get_key()
        if provider == "qc" and not current_key:
            return JSONResponse(
                status_code=429,
                content={"error": {"message": f"All QC keys are exhausted for model '{upstream_model}'."}},
            )
        headers["Authorization"] = f"Bearer {current_key}"
        start_req_time = time.time()

        try:
            if payload.get("stream"):
                print(f"[STREAM-INIT] Entering stream mode for model: {requested_model}, stream={payload.get('stream')}", flush=True)
                client = _get_upstream_client()
                upstream_request = client.build_request(
                    "POST", upstream_endpoint, headers=headers, json=payload
                )
                resp = await client.send(upstream_request, stream=True)
                if resp.status_code == 200:
                    return StreamingResponse(
                        _relay_openai_upstream_stream(
                            resp,
                            requested_model=requested_model,
                            display_model=display_model,
                            current_key=current_key,
                            provider=provider,
                            started_at=start_req_time,
                            request=request,
                        ),
                        media_type="text/event-stream",
                    )

                error_bytes = await resp.aread()
                await resp.aclose()
                try:
                    content = json.loads(error_bytes)
                except Exception:
                    content = {"error": {"message": error_bytes.decode(errors="replace")}}
                last_status = resp.status_code
                last_content = content
                add_request_log(
                    requested_model,
                    resp.status_code,
                    current_key,
                    True,
                    int((time.time() - start_req_time) * 1000),
                )
                if resp.status_code in _RETRYABLE_UPSTREAM_STATUSES and attempt < len(api_keys_to_use) - 1:
                    if provider == "qc":
                        _rotate_qc_after_failure(upstream_model, current_key, resp.status_code, content)
                    else:
                        rotate()
                    continue
                return JSONResponse(status_code=resp.status_code, content=content)

            async with _borrow_upstream_client() as client:
                resp = await client.post(upstream_endpoint, headers=headers, json=payload)

            effective_status = resp.status_code
            try:
                content = resp.json()
            except Exception:
                aggregated = _aggregate_openai_sse(resp.text, requested_model) if resp.status_code == 200 else None
                if aggregated is not None:
                    print(f"[CHAT-COMPLETIONS] {requested_model}: upstream ignored stream=false, reconstructed from SSE", flush=True)
                    content = aggregated
                else:
                    content = {"error": {"message": resp.text}}
                    if resp.status_code == 200:
                        # Upstream claimed success but sent something we
                        # couldn't parse or reconstruct -- don't let that
                        # masquerade as a real 200 to the client.
                        effective_status = 502

            if effective_status == 200:
                choice = (content.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                real_content = message.get("content")

                # Strip thinking tags for -thinking/-agentic models in OpenAI format.
                if real_content and isinstance(real_content, str):
                    if requested_model.endswith(("-thinking", "-agentic", "-thinking-agentic")):
                        import re
                        real_content = re.sub(r'<thinking>.*?</thinking>\s*', '', real_content, flags=re.DOTALL)
                        if "choices" in content and len(content["choices"]) > 0:
                            if "message" in content["choices"][0]:
                                content["choices"][0]["message"]["content"] = real_content

                usage = content.get("usage", {}) if isinstance(content, dict) else {}
                input_tokens = usage.get("prompt_tokens", 0) or 0
                output_tokens = usage.get("completion_tokens", 0) or 0
                add_request_log(requested_model, 200, current_key, False, int((time.time() - start_req_time) * 1000), input_tokens, output_tokens)
                await _bill_router_key(request, input_tokens + output_tokens)
                await sse_broadcaster.broadcast("log", recent_requests[0] if recent_requests else {})
                await sse_broadcaster.broadcast("status", await _build_status_dict(include_details=False))
                # Upstream reports its own model name; swap in the alias so the
                # rename is consistent with what /v1/models advertised.
                if isinstance(content, dict) and content.get("model"):
                    content["model"] = display_model
                return JSONResponse(content)

            last_status = effective_status
            last_content = content
            add_request_log(requested_model, effective_status, current_key, True, int((time.time() - start_req_time) * 1000))
            if effective_status in _RETRYABLE_UPSTREAM_STATUSES and attempt < len(api_keys_to_use) - 1:
                if provider == "qc":
                    _rotate_qc_after_failure(upstream_model, current_key, effective_status, content)
                else:
                    rotate()
                continue
            return JSONResponse(status_code=effective_status, content=content)
        except Exception as e:
            last_status = 500
            last_content = {"error": {"message": str(e)}}
            add_request_log(requested_model, 500, current_key, True, int((time.time() - start_req_time) * 1000))
            if attempt < len(api_keys_to_use) - 1:
                if provider == "qc":
                    rotate_qc_key_for_model(upstream_model, after_key=current_key)
                else:
                    rotate()
                continue
            return JSONResponse(status_code=500, content=last_content)

    return JSONResponse(status_code=last_status, content=last_content)
