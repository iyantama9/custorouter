import json
import ipaddress
import os
import secrets
import time
from typing import AsyncGenerator

from fastapi import APIRouter, Request, Body, Cookie, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import httpx
from redis.exceptions import RedisError

import asyncio

import app.config as config_module
from app.config import (
    BLUESMINDS_BASE_URL, NARA_BASE_URL, DAHL_BASE_URL,
    QWEN_CLOUD_BASE_URL, MARKETKU_BASE_URL,
    recent_requests,
    add_api_key, remove_api_key, bulk_remove_api_keys, reset_key_status, get_masked_keys, set_active_key,
    add_custom_provider, remove_provider,
    ADMIN_USERNAME, verify_admin_password, get_paginated_logs,
)
from app.sse import sse_broadcaster
from app.redis_store import (
    clear_login_attempts,
    consume_login_attempt,
    create_session,
    get_cached_json,
    revoke_session,
    set_cached_json,
    validate_session,
)
from app.database import (
    fetch, fetch_one, create_router_api_key, get_router_api_keys, delete_router_api_key,
    update_router_api_key, reset_router_key_usage,
)


router = APIRouter()
templates = Jinja2Templates(directory="templates")


_password_checks = asyncio.Semaphore(3)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_GLOBAL_MAX_ATTEMPTS = 30
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_GLOBAL_WINDOW_SECONDS = 60
_SESSION_TTL_SECONDS = int(os.getenv("DASHBOARD_SESSION_TTL_SECONDS", str(14 * 24 * 60 * 60)))
_TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"
_TRUSTED_PROXY_IPS = {
    address.strip() for address in os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
    if address.strip()
}


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    # Only a local reverse proxy may supply the client address. X-Forwarded-For
    # is intentionally ignored: clients can prepend arbitrary entries to it.
    if _TRUST_PROXY_HEADERS and peer in _TRUSTED_PROXY_IPS:
        forwarded = request.headers.get("x-real-ip", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


async def _valid_session(token: str | None) -> bool:
    return await validate_session(token, _SESSION_TTL_SECONDS)


async def require_auth(session_token: str = Cookie(default=None)):
    try:
        valid = await _valid_session(session_token)
    except (RedisError, RuntimeError):
        # Never degrade to an in-memory or fail-open auth path. A transient
        # Redis outage should be visible as service unavailable, not a login
        # failure that trains the user to repeatedly enter credentials.
        raise HTTPException(status_code=503, detail="Authentication service temporarily unavailable")
    if not valid:
        raise HTTPException(status_code=401, detail="Not authenticated")


async def _build_status_dict():
    uptime_seconds = int(time.time() - config_module.START_TIME)
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    uptime_str = f"{hours}h {minutes}m {seconds}s"
    _all_keys = get_masked_keys()
    available_keys = sum(1 for k in _all_keys if k['status'] in ('Active', 'Standby'))
    return {
        "status": "online",
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "total_requests": config_module.total_requests,
        "failover_count": config_module.failover_count,
        "total_tokens": config_module.total_tokens,
        "available_keys": available_keys,
        "total_keys": len(_all_keys),
        "keys": _all_keys,
        "recent_requests": recent_requests,
        "migration": config_module.migration_status(),
        # Lets the dashboard notice a provider/model catalog change and
        # refetch. This is the copy that matters most: every add/remove
        # provider and add/remove key endpoint broadcasts through here.
        "providers_signature": config_module.providers_signature(),
    }


@router.get("/", response_class=RedirectResponse)
async def get_root():
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def get_login(request: Request, session_token: str = Cookie(default=None)):
    if await _valid_session(session_token):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request, session_token: str = Cookie(default=None)):
    if not await _valid_session(session_token):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request=request, name="dashboard.html")


@router.post("/api/login")
async def api_login(request: Request):
    client_ip = _client_ip(request)
    try:
        allowed = await consume_login_attempt(
            client_ip,
            per_ip_limit=_LOGIN_MAX_ATTEMPTS,
            per_ip_window=_LOGIN_WINDOW_SECONDS,
            global_limit=_LOGIN_GLOBAL_MAX_ATTEMPTS,
            global_window=_LOGIN_GLOBAL_WINDOW_SECONDS,
        )
    except (RedisError, RuntimeError):
        return JSONResponse(status_code=503, content={"success": False, "message": "Authentication service temporarily unavailable."})
    if not allowed:
        return JSONResponse(status_code=429, content={"success": False, "message": "Too many login attempts. Try again later."})
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            return JSONResponse(status_code=413, content={"success": False, "message": "Request too large"})
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid credentials"})
    username = payload.get("username", "")
    password = payload.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 100 or len(password) > 1024:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid credentials"})
    # bcrypt is intentionally expensive; run it outside the event loop so a
    # login attempt cannot pause proxy traffic for every other client.
    async with _password_checks:
        try:
            password_ok = await asyncio.to_thread(verify_admin_password, password)
        except ValueError:
            password_ok = False
    if username == ADMIN_USERNAME and password_ok:
        try:
            await clear_login_attempts(client_ip)
        except (RedisError, RuntimeError):
            return JSONResponse(status_code=503, content={"success": False, "message": "Authentication service temporarily unavailable."})
        token = secrets.token_urlsafe(32)
        try:
            await create_session(token, _SESSION_TTL_SECONDS)
        except (RedisError, RuntimeError):
            return JSONResponse(status_code=503, content={"success": False, "message": "Authentication service temporarily unavailable."})
        return JSONResponse(
            content={"success": True},
            headers={
                "Set-Cookie": f"session_token={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={_SESSION_TTL_SECONDS}; Priority=High"
            }
        )
    return JSONResponse(status_code=401, content={"success": False, "message": "Invalid credentials"})


@router.post("/api/logout")
async def api_logout(session_token: str = Cookie(default=None)):
    try:
        await revoke_session(session_token)
    except (RedisError, RuntimeError):
        return JSONResponse(status_code=503, content={"success": False, "message": "Authentication service temporarily unavailable."})
    return JSONResponse(
        content={"success": True},
        headers={
            "Set-Cookie": "session_token=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"
        }
    )


@router.get("/api/status")
async def get_status(user: None = Depends(require_auth)):
    return await _build_status_dict()


@router.get("/api/operations/drain")
async def get_migration_drain(user: None = Depends(require_auth)):
    """Show whether a planned cutover is accepting new inference work."""
    return config_module.migration_status()


@router.put("/api/operations/drain")
async def set_migration_drain(payload: dict = Body(...), user: None = Depends(require_auth)):
    """Open/close the migration drain gate; existing streams keep running."""
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse(status_code=400, content={"error": "enabled must be a boolean"})
    status = await config_module.set_migration_drain(enabled)
    await sse_broadcaster.broadcast("status", await _build_status_dict())
    return status


@router.get("/api/logs")
async def api_logs(
    user: None = Depends(require_auth),
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=5, le=100),
    search: str = Query(""),
    sort_by: str = Query("created_at"),
    sort_order: str = Query("DESC")
):
    # A dashboard can issue the same query from polling and a live-event
    # refresh at nearly the same time. A tiny shared cache removes duplicate
    # PostgreSQL scans without making the activity view materially stale.
    cache_key = f"dashboard-logs:v1:{page}:{per_page}:{search}:{sort_by}:{sort_order}"
    try:
        cached = await get_cached_json(cache_key)
        if cached is not None:
            return cached
    except (RedisError, RuntimeError):
        pass
    result = await get_paginated_logs(page, per_page, search, sort_by, sort_order)
    try:
        await set_cached_json(cache_key, result, 2)
    except (RedisError, RuntimeError):
        pass
    return result


async def _probe_provider(client: httpx.AsyncClient, prefix: str, info: dict, key: str) -> str | None:
    """Probe a provider's model endpoint with its configured auth headers."""
    try:
        r = await client.get(
            f"{info['base_url']}/models",
            headers=config_module.custom_provider_headers(info, key), timeout=6.0,
        )
        return prefix if r.status_code == 200 else None
    except Exception:
        return None


async def detect_provider_for_key(key: str) -> list[str]:
    """
    Test a key against every configured provider (built-in + custom) at once
    and return every prefix that accepted it. Real network probes, not a
    guess from the key's shape -- provider key formats overlap too much
    (most are just "sk-...") for pattern matching to be reliable.
    """
    candidates = {prefix: {"base_url": url, "api_format": "openai"}
                  for prefix, url in config_module.BUILTIN_PROVIDER_BASE_URLS.items()}
    candidates.update(config_module.CUSTOM_PROVIDERS)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[
            _probe_provider(client, prefix, info, key) for prefix, info in candidates.items()
        ])
    return [r for r in results if r]


@router.post("/api/keys/detect")
async def api_detect_key(payload: dict = Body(...), user: None = Depends(require_auth)):
    key = payload.get("key", "").strip()
    if not key:
        return JSONResponse(status_code=400, content={"error": "key is required"})
    matches = await detect_provider_for_key(key)
    return {"matches": matches}


@router.post("/api/keys")
async def add_key_endpoint(payload: dict = Body(...), user: None = Depends(require_auth)):
    key = payload.get("key", "").strip()
    key_type = payload.get("type", "auto")

    if key_type == "auto":
        matches = await detect_provider_for_key(key)
        if len(matches) == 1:
            key_type = matches[0]
        elif len(matches) > 1:
            return {"success": False, "message": f"Key matched multiple providers ({', '.join(matches)}) -- pick one explicitly.", "matches": matches}
        else:
            return {"success": False, "message": "Couldn't detect a provider for this key -- pick one explicitly.", "matches": []}

    success, msg = add_api_key(key, key_type)
    if success:
        # A key for a custom provider is useless without its model catalog --
        # the "Add Provider" flow does this automatically, but a key added
        # from this generic form (or a second/replacement key) needs it too.
        if key_type in config_module.CUSTOM_PROVIDERS:
            refreshed, refresh_msg = await config_module.refresh_custom_provider_models(key_type)
            if refreshed:
                msg = f"{msg} ({refresh_msg})"
        await sse_broadcaster.broadcast("status", await _build_status_dict())
    return {"success": success, "message": msg}


@router.delete("/api/keys")
async def remove_key_endpoint(payload: dict = Body(...), user: None = Depends(require_auth)):
    prefix = payload.get("key_prefix", "")
    success, msg = remove_api_key(prefix)
    if success:
        await sse_broadcaster.broadcast("status", await _build_status_dict())
    return {"success": success, "message": msg}


@router.post("/api/keys/bulk-delete")
async def bulk_delete_keys_endpoint(payload: dict = Body(...), user: None = Depends(require_auth)):
    prefixes = payload.get("key_prefixes") or []
    if not isinstance(prefixes, list) or not prefixes:
        return JSONResponse(status_code=400, content={"error": "key_prefixes must be a non-empty list"})
    removed, failed = bulk_remove_api_keys(prefixes)
    if removed:
        await sse_broadcaster.broadcast("status", await _build_status_dict())
    return {"removed": removed, "failed": failed, "removed_count": len(removed), "failed_count": len(failed)}


@router.get("/api/providers")
async def list_providers_endpoint(user: None = Depends(require_auth)):
    keys = get_masked_keys()
    key_counts: dict[str, int] = {}
    for k in keys:
        key_counts[k["provider"]] = key_counts.get(k["provider"], 0) + 1

    builtins = [
        {
            "prefix": prefix,
            "name": config_module.BUILTIN_PROVIDER_NAMES[prefix],
            "base_url": config_module.BUILTIN_PROVIDER_BASE_URLS[prefix],
            "api_format": "openai",
            "builtin": True,
            "disabled": prefix in config_module.DISABLED_PROVIDERS,
            "key_count": key_counts.get(prefix, 0),
        }
        for prefix in sorted(config_module.BUILTIN_PROVIDER_PREFIXES)
    ]
    customs = [
        {
            "prefix": prefix,
            "name": info["name"],
            "base_url": info["base_url"],
            "api_format": info["api_format"],
            "auth_header": info.get("auth_header", "bearer"),
            "anthropic_version": info.get("anthropic_version", "2023-06-01"),
            "builtin": False,
            "disabled": False,
            "key_count": key_counts.get(prefix, 0),
            "model_count": len(info.get("models") or []),
        }
        for prefix, info in config_module.CUSTOM_PROVIDERS.items()
    ]
    return {"providers": builtins + customs}


@router.post("/api/providers")
async def add_provider_endpoint(payload: dict = Body(...), user: None = Depends(require_auth)):
    prefix = payload.get("prefix", "")
    name = payload.get("name", "")
    base_url = payload.get("base_url", "")
    api_format = payload.get("api_format", "openai")
    auth_header = payload.get("auth_header", "bearer")
    anthropic_version = payload.get("anthropic_version", "2023-06-01")
    api_key = (payload.get("api_key") or "").strip()

    success, msg = await add_custom_provider(prefix, name, base_url, api_format, auth_header, anthropic_version)
    if not success:
        return {"success": False, "message": msg}

    prefix = prefix.strip().lower()
    models_msg = None
    if api_key:
        key_ok, key_msg = add_api_key(api_key, prefix)
        if key_ok:
            refreshed, refresh_msg = await config_module.refresh_custom_provider_models(prefix)
            models_msg = refresh_msg if refreshed else f"Provider added, but couldn't fetch its models: {refresh_msg}"
        else:
            models_msg = f"Provider added, but the key failed: {key_msg}"

    await sse_broadcaster.broadcast("status", await _build_status_dict())
    return {"success": True, "message": models_msg or msg}


@router.post("/api/providers/{prefix}/refresh-models")
async def refresh_provider_models_endpoint(prefix: str, user: None = Depends(require_auth)):
    success, msg = await config_module.refresh_custom_provider_models(prefix)
    return {"success": success, "message": msg}


@router.put("/api/providers/{prefix}/models")
async def set_provider_models_endpoint(prefix: str, payload: dict = Body(...), user: None = Depends(require_auth)):
    models = payload.get("models") or []
    if not isinstance(models, list):
        return JSONResponse(status_code=400, content={"error": "models must be a list"})
    success, msg = await config_module.set_custom_provider_models(prefix, models)
    return {"success": success, "message": msg}


@router.delete("/api/providers/{prefix}")
async def remove_provider_endpoint(prefix: str, user: None = Depends(require_auth)):
    success, msg = await remove_provider(prefix)
    if success:
        await sse_broadcaster.broadcast("status", await _build_status_dict())
    return {"success": success, "message": msg}


@router.post("/api/keys/reset")
async def reset_key_endpoint(payload: dict = Body(...), user: None = Depends(require_auth)):
    prefix = payload.get("key_prefix", "")
    success, msg = reset_key_status(prefix)
    if success:
        await sse_broadcaster.broadcast("status", await _build_status_dict())
    return {"success": success, "message": msg}


@router.post("/api/keys/set_active")
async def api_set_active_key(payload: dict = Body(...), user: None = Depends(require_auth)):
    prefix = payload.get("key_prefix", "")
    provider = payload.get("provider", None)
    success, msg = set_active_key(prefix, provider)
    if success:
        await sse_broadcaster.broadcast("status", await _build_status_dict())
    return {"success": success, "message": msg}


@router.get("/api/models")
async def api_get_models(user: None = Depends(require_auth)):
    builtin = {
        "bluesminds": ("bm", [f"bm/{m}" for m in config_module.BLUESMINDS_MODELS]),
        "bynara": ("nry", [f"nry/{m}" for m in config_module.NARA_MODELS]),
        "dahl": ("dahl", [f"dh/{m}" for m in config_module.DAHL_MODELS_SHORT]),
        "qwen_cloud": ("qc", [f"qc/{m}" for m in config_module.QWEN_CLOUD_MODELS]),
        "marketku": ("marketku", [f"mk/{m}" for m in config_module.MARKETKU_MODELS]),
    }
    # A removed built-in keeps its cached model list around (so re-adding a
    # key doesn't need a fresh /models refresh to work again) -- just don't
    # advertise it anywhere while it's disabled.
    result = {
        key: ([] if prefix in config_module.DISABLED_PROVIDERS else models)
        for key, (prefix, models) in builtin.items()
    }
    # Custom providers are keyed by their own prefix so the Playground picker
    # can group and label them without any hardcoded knowledge of them.
    for prefix, info in config_module.CUSTOM_PROVIDERS.items():
        result[prefix] = [f"{prefix}/{m}" for m in info.get("models") or []]
    return result


@router.post("/api/models/refresh")
async def api_refresh_models(user: None = Depends(require_auth)):
    """Query all provider APIs and update model lists in .env file."""
    from pathlib import Path

    updated_count = 0
    updates = {}
    errors = {}

    # Provider to base_url mapping
    provider_base_urls = {
        "bm": BLUESMINDS_BASE_URL,
        "nry": NARA_BASE_URL,
        "dahl": DAHL_BASE_URL,
        "qc": QWEN_CLOUD_BASE_URL,
        "marketku": MARKETKU_BASE_URL,
    }

    # Provider to env var name mapping
    provider_env_vars = {
        "bm": "BLUESMINDS_MODELS",
        "nry": "NARA_MODELS",
        "dahl": "DAHL_MODELS",
        "qc": "QWEN_CLOUD_MODELS",
        "marketku": "MARKETKU_MODELS",
    }

    # Provider to routing prefix mapping (used to strip self-namespaced ids
    # some providers return, e.g. marketku returns "mk/auto" instead of "auto")
    provider_prefixes = {
        "bm": "bm",
        "nry": "nry",
        "dahl": "dh",
        "qc": "qc",
        "marketku": "mk",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Get distinct providers from api_keys
        providers = await fetch("SELECT DISTINCT provider FROM api_keys ORDER BY provider")

        for provider_row in providers:
            provider = provider_row["provider"]
            base_url = provider_base_urls.get(provider)
            env_var = provider_env_vars.get(provider)

            if not base_url or not env_var:
                errors[provider] = "No base URL or env var configured"
                continue

            try:
                # Get any available key for this provider
                key_row = await fetch_one(
                    "SELECT key_value FROM api_keys WHERE provider = $1 LIMIT 1",
                    provider
                )

                if not key_row:
                    errors[provider] = "No API key found in database"
                    continue

                api_key = key_row["key_value"]

                # Scan /models endpoint
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                r = await client.get(f"{base_url}/models", headers=headers)

                if r.status_code == 200:
                    data = r.json()
                    models = [m["id"] for m in data.get("data", [])]

                    # Some providers return model ids already namespaced with
                    # our own routing prefix (e.g. marketku returns "mk/auto"),
                    # which would double up once we prefix again for display/dispatch.
                    prefix = f"{provider_prefixes.get(provider, provider)}/"
                    models = [m[len(prefix):] if m.startswith(prefix) else m for m in models]

                    # Deduplicate
                    models = list(dict.fromkeys(models))

                    if models:
                        updates[env_var] = ",".join(models)
                        updated_count += 1
                        print(f"[REFRESH] {provider}: Found {len(models)} models")
                    else:
                        errors[provider] = "No models returned from API"
                else:
                    errors[provider] = f"HTTP {r.status_code}: {r.text[:200]}"
                    print(f"[REFRESH] {provider}: HTTP {r.status_code} - {r.text[:500]}")

            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                errors[provider] = error_msg
                print(f"[REFRESH] {provider}: {error_msg}")

    # Update .env file
    if updates:
        env_path = Path(".env")
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            new_lines = []
            updated_vars = set()

            # Update existing lines
            for line in lines:
                updated = False
                for key, value in updates.items():
                    if line.startswith(f"{key}="):
                        new_lines.append(f"{key}={value}")
                        updated_vars.add(key)
                        updated = True
                        break
                if not updated:
                    new_lines.append(line)

            # Append new vars that weren't in the file
            for key, value in updates.items():
                if key not in updated_vars:
                    new_lines.append(f"{key}={value}")

            env_path.write_text("\n".join(new_lines) + "\n")
            print(f"[REFRESH] Updated .env with {len(updates)} provider model lists")

        # Reload models in config so changes take effect immediately
        from app import config
        config.reload_models_from_env()

    return {
        "success": True,
        "updated": updated_count,
        "providers": list(updates.keys()),
        "errors": errors,
        "details": {k: len(v.split(",")) for k, v in updates.items()}
    }


def _json_safe_row(row):
    data = dict(row)
    for key, value in data.items():
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


_ROUTING_RANGES = {"today": None, "24h": 1, "7d": 7, "30d": 30, "60d": 60}


@router.get("/api/routing/stats")
async def routing_stats_endpoint(range: str = Query("today"), user: None = Depends(require_auth)):
    """Traffic totals + per-provider breakdown for the routing view."""
    import datetime

    if range not in _ROUTING_RANGES:
        return JSONResponse(status_code=400, content={"error": f"range must be one of {list(_ROUTING_RANGES)}"})

    cache_key = f"routing-stats:v1:{range}"
    try:
        cached = await get_cached_json(cache_key)
        if cached is not None:
            return cached
    except (RedisError, RuntimeError):
        pass

    # The rest of the app reports timestamps in UTC+7, so "today" means
    # midnight local, not midnight UTC.
    now = datetime.datetime.now(datetime.timezone.utc)
    if range == "today":
        local = now.astimezone(datetime.timezone(datetime.timedelta(hours=7)))
        cutoff = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc)
    else:
        cutoff = now - datetime.timedelta(days=_ROUTING_RANGES[range])

    totals_row = await fetch_one("""
        SELECT
            COUNT(*)                            AS requests,
            COALESCE(SUM(input_tokens), 0)      AS input_tokens,
            COALESCE(SUM(output_tokens), 0)     AS output_tokens,
            COALESCE(SUM(cached_tokens), 0)     AS cached_tokens,
            COALESCE(AVG(latency_ms), 0)        AS avg_latency_ms,
            COUNT(*) FILTER (WHERE status_code >= 400) AS errors
        FROM request_logs
        WHERE created_at >= $1
    """, cutoff)

    per_provider = await fetch("""
        SELECT
            COALESCE(provider, CASE split_part(model, '/', 1)
                WHEN 'dh' THEN 'dahl' WHEN 'mk' THEN 'marketku'
                ELSE split_part(model, '/', 1) END) AS provider,
            COUNT(*)                        AS requests,
            COALESCE(SUM(input_tokens), 0)  AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
            COALESCE(AVG(latency_ms), 0)    AS avg_latency_ms,
            COUNT(*) FILTER (WHERE status_code >= 400) AS errors,
            MAX(created_at)                 AS last_used_at
        FROM request_logs
        WHERE created_at >= $1
        GROUP BY 1
    """, cutoff)
    stats_by_provider = {r["provider"]: r for r in per_provider if r["provider"]}

    key_counts: dict[str, int] = {}
    for k in get_masked_keys():
        key_counts[k["provider"]] = key_counts.get(k["provider"], 0) + 1

    providers = []
    for prefix in sorted(config_module.BUILTIN_PROVIDER_PREFIXES):
        if prefix in config_module.DISABLED_PROVIDERS:
            continue
        providers.append((prefix, config_module.BUILTIN_PROVIDER_NAMES[prefix], config_module.BUILTIN_PROVIDER_BASE_URLS[prefix]))
    for prefix, info in config_module.CUSTOM_PROVIDERS.items():
        providers.append((prefix, info["name"], info["base_url"]))

    provider_rows = []
    for prefix, name, base_url in providers:
        s = stats_by_provider.get(prefix)
        last_used = s["last_used_at"] if s and s["last_used_at"] else None
        provider_rows.append({
            "prefix": prefix,
            "name": name,
            "base_url": base_url,
            "key_count": key_counts.get(prefix, 0),
            "requests": int(s["requests"]) if s else 0,
            "input_tokens": int(s["input_tokens"]) if s else 0,
            "output_tokens": int(s["output_tokens"]) if s else 0,
            "cached_tokens": int(s["cached_tokens"]) if s else 0,
            "avg_latency_ms": int(s["avg_latency_ms"]) if s else 0,
            "errors": int(s["errors"]) if s else 0,
            "last_used_at": last_used.isoformat() if last_used else None,
        })

    recent = await fetch("""
        SELECT model, status_code, input_tokens, output_tokens, cached_tokens, latency_ms,
               COALESCE(provider, CASE split_part(model, '/', 1)
                WHEN 'dh' THEN 'dahl' WHEN 'mk' THEN 'marketku'
                ELSE split_part(model, '/', 1) END) AS provider, created_at
        FROM request_logs
        WHERE created_at >= $1
        ORDER BY created_at DESC
        LIMIT 25
    """, cutoff)

    result = {
        "range": range,
        "since": cutoff.isoformat(),
        "totals": {
            "requests": int(totals_row["requests"]),
            "input_tokens": int(totals_row["input_tokens"]),
            "output_tokens": int(totals_row["output_tokens"]),
            "cached_tokens": int(totals_row["cached_tokens"]),
            "avg_latency_ms": int(totals_row["avg_latency_ms"]),
            "errors": int(totals_row["errors"]),
        },
        "providers": provider_rows,
        "recent": [
            {
                "model": r["model"],
                "status_code": r["status_code"],
                "provider": r["provider"],
                "input_tokens": r["input_tokens"] or 0,
                "output_tokens": r["output_tokens"] or 0,
                "cached_tokens": r["cached_tokens"] or 0,
                "latency_ms": r["latency_ms"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in recent
        ],
    }
    try:
        await set_cached_json(cache_key, result, 2)
    except (RedisError, RuntimeError):
        pass
    return result


@router.get("/api/sse")
async def sse_endpoint(request: Request, user: None = Depends(require_auth)):
    async def event_generator() -> AsyncGenerator[str, None]:
        q = sse_broadcaster.connect()
        try:
            status = await _build_status_dict()
            yield f"data: {json.dumps({'type': 'status', 'payload': status})}\n\n"
            while True:
                try:
                    data = await __import__('asyncio').wait_for(q.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                except __import__('asyncio').TimeoutError:
                    yield ":keepalive\n\n"
        finally:
            sse_broadcaster.disconnect(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/api/router-keys")
async def api_get_router_keys(user: None = Depends(require_auth)):
    """Get all router API keys."""
    keys = await get_router_api_keys()
    return {"keys": [_json_safe_row(k) for k in keys]}


class _KeySettingsError(Exception):
    """Raised when a key's settings don't validate; message goes to the client."""


def _parse_key_settings(payload: dict):
    """
    Validate the tunable parts of a router key. Shared by create and update so
    an edited key can't end up in a state the create endpoint would refuse.
    """
    import datetime

    try:
        expires_in_days = int(payload.get("expires_in_days") or 0)
        token_quota = int(payload.get("token_quota") or 0)
    except (TypeError, ValueError):
        raise _KeySettingsError("expires_in_days and token_quota must be numbers")
    if expires_in_days < 0 or token_quota < 0:
        raise _KeySettingsError("expires_in_days and token_quota cannot be negative")

    # 0 / omitted means "never expires" and "no quota", matching how existing
    # keys behave so nothing changes for them.
    expires_at = None
    if expires_in_days > 0:
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=expires_in_days)

    models = payload.get("allowed_models") or []
    if not isinstance(models, list):
        raise _KeySettingsError("allowed_models must be a list")
    allowed = sorted({str(m).strip() for m in models if str(m).strip()})

    # Optional per-model system prompt, scoped to this key only.
    prompts_in = payload.get("model_prompts") or {}
    if not isinstance(prompts_in, dict):
        raise _KeySettingsError("model_prompts must be an object")
    prompts = {}
    for model, text in prompts_in.items():
        text = str(text or "").strip()
        if not text:
            continue
        if len(text) > 8000:
            raise _KeySettingsError(f"Prompt for '{model}' is too long (max 8000 characters)")
        # A prompt for a model the key can't call would silently never fire.
        if allowed and str(model) not in allowed:
            raise _KeySettingsError(f"'{model}' has a prompt but isn't in the allowed models")
        prompts[str(model)] = text

    # Optional per-model rename, scoped to this key.
    aliases_in = payload.get("model_aliases") or {}
    if not isinstance(aliases_in, dict):
        raise _KeySettingsError("model_aliases must be an object")
    aliases, seen = {}, set()
    for model, alias in aliases_in.items():
        alias = str(alias or "").strip()
        if not alias:
            continue
        model = str(model)
        if allowed and model not in allowed:
            raise _KeySettingsError(f"'{model}' has a rename but isn't in the allowed models")
        if len(alias) > 100:
            raise _KeySettingsError(f"Rename for '{model}' is too long (max 100 characters)")
        # Two models answering to the same name, or a name that already belongs
        # to a different real model, would make routing ambiguous.
        if alias in seen:
            raise _KeySettingsError(f"'{alias}' is used as a rename for more than one model")
        if alias in aliases_in and alias != model:
            raise _KeySettingsError(f"'{alias}' is both a rename and a real model in this key")
        seen.add(alias)
        aliases[model] = alias

    return {
        "expires_at": expires_at,
        "expires_in_days": expires_in_days,
        "token_quota": token_quota,
        "allowed_models": ",".join(allowed),
        "model_prompts": json.dumps(prompts),
        "model_aliases": json.dumps(aliases),
    }


@router.post("/api/router-keys")
async def api_create_router_key(payload: dict = Body(...), user: None = Depends(require_auth)):
    """Generate a new router API key."""
    key_name = payload.get("key_name", "").strip()
    if not key_name:
        return JSONResponse(status_code=400, content={"success": False, "message": "Key name is required"})
    try:
        s = _parse_key_settings(payload)
    except _KeySettingsError as e:
        return JSONResponse(status_code=400, content={"success": False, "message": str(e)})

    key = await create_router_api_key(
        key_name, s["expires_at"], s["token_quota"],
        s["allowed_models"], s["model_prompts"], s["model_aliases"],
    )
    return {"success": True, "key": _json_safe_row(key)}


@router.put("/api/router-keys/{key_id}")
async def api_update_router_key(key_id: int, payload: dict = Body(...), user: None = Depends(require_auth)):
    """
    Edit an existing key's settings in place.

    The secret itself never changes, so whoever already holds it keeps
    working -- only what the key is allowed to do changes.
    """
    key_name = payload.get("key_name", "").strip()
    if not key_name:
        return JSONResponse(status_code=400, content={"success": False, "message": "Key name is required"})
    try:
        s = _parse_key_settings(payload)
    except _KeySettingsError as e:
        return JSONResponse(status_code=400, content={"success": False, "message": str(e)})

    # An untouched expiry ("keep as-is") must not silently become "never".
    keep_expiry = payload.get("keep_expiry") is True
    updated = await update_router_api_key(
        key_id, key_name, s["expires_at"], s["token_quota"],
        s["allowed_models"], s["model_prompts"], s["model_aliases"],
        keep_expiry=keep_expiry,
    )
    if not updated:
        return JSONResponse(status_code=404, content={"success": False, "message": "Key not found"})
    if payload.get("reset_usage") is True:
        updated = await reset_router_key_usage(key_id)
    return {"success": True, "key": _json_safe_row(updated)}


@router.delete("/api/router-keys/{key_id}")
async def api_delete_router_key(key_id: int, user: None = Depends(require_auth)):
    """Delete a router API key."""
    await delete_router_api_key(key_id)
    return {"success": True, "message": "Key deleted"}
