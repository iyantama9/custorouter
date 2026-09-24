# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi",
#   "httpx",
#   "uvicorn",
#   "python-dotenv",
#   "asyncpg",
#   "jinja2",
#   "bcrypt",
# ]
# ///

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app.database import init_db, close_db
from app.redis_store import (
    init_redis, close_redis, redis_available, ensure_request_log_consumer_group,
)
import app.config as config_module
from app.config import init_state_from_db, auto_reset_limited_keys, PORT, SSL_KEYFILE, SSL_CERTFILE, ROUTER_DOMAIN
from app.sse import sse_broadcaster
from app.request_log_worker import run_request_log_worker
from app.routers import admin, playground, proxy


async def _build_status_dict():
    import time
    from app.config import get_masked_keys, total_requests, failover_count, total_tokens, START_TIME, recent_requests
    uptime_seconds = int(time.time() - START_TIME)
    _all_keys = get_masked_keys()
    available_keys = sum(1 for k in _all_keys if k['status'] in ('Active', 'Standby'))
    return {
        "status": "online",
        "uptime_seconds": uptime_seconds,
        "total_requests": total_requests,
        "failover_count": failover_count,
        "total_tokens": total_tokens,
        "available_keys": available_keys,
        "total_keys": len(_all_keys),
        "keys": _all_keys,
        "recent_requests": recent_requests,
        "migration": config_module.migration_status(),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    await proxy.init_http_clients()
    reset_task = None
    request_log_task = None
    try:
        await init_db()
        await init_redis()
        # Create the group before declaring application startup complete. A
        # detached worker that fails before it reaches XGROUP would otherwise
        # be silent and make telemetry durability look healthy when it is not.
        await ensure_request_log_consumer_group()
        await sse_broadcaster.start()
        await init_state_from_db()
        request_log_task = asyncio.create_task(run_request_log_worker())
        print("[INIT] Database and Redis connected; state loaded")

        async def _auto_reset_loop():
            while True:
                await asyncio.sleep(60)
                try:
                    reset = await auto_reset_limited_keys()
                    if reset:
                        await sse_broadcaster.broadcast("status", await _build_status_dict())
                except Exception as e:
                    print(f"[AUTO-RESET] Error: {e}")

        reset_task = asyncio.create_task(_auto_reset_loop())
        yield
    finally:
        if request_log_task:
            request_log_task.cancel()
            await asyncio.gather(request_log_task, return_exceptions=True)
        if reset_task:
            reset_task.cancel()
            await asyncio.gather(reset_task, return_exceptions=True)
        await proxy.close_http_clients()
        await sse_broadcaster.stop()
        await close_redis()
        await close_db()
        print("[INIT] Database connection closed")


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)


@app.get("/health", include_in_schema=False)
async def health_check():
    """Lightweight liveness endpoint for the container orchestrator."""
    if not await redis_available():
        return JSONResponse(status_code=503, content={"status": "degraded", "redis": "unavailable"})
    return {"status": "ok", "redis": "ok"}


_INFERENCE_REQUEST_PATHS = {
    "/v1/messages",
    "/v1/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/v1/messages/count_tokens",
    "/v1/chat/completions",
    "/chat/completions",
}


def _is_inference_request(request) -> bool:
    return request.method == "POST" and request.url.path in _INFERENCE_REQUEST_PATHS


@app.middleware("http")
async def security_and_observability_headers(request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    is_inference = _is_inference_request(request)
    tracking_inference = False

    # Cookie-authenticated admin mutations must originate from this site.
    # Bearer-authenticated proxy APIs are unaffected.
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path.startswith("/api/")
        and request.cookies.get("session_token")
    ):
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(status_code=403, content={"error": "Cross-site request blocked."})
        origin = request.headers.get("origin")
        if origin:
            # Do not trust X-Forwarded-Host from a client; it can be spoofed to
            # make an attacker-controlled Origin appear same-site.
            host = request.headers.get("host")
            allowed_origins = {f"https://{host}", f"http://{host}"}
            if origin.rstrip("/") not in allowed_origins:
                return JSONResponse(status_code=403, content={"error": "Invalid request origin."})

    if is_inference and config_module.MIGRATION_DRAIN_ENABLED:
        response = JSONResponse(
            status_code=503,
            content={"error": {"message": "Router is draining for planned migration. Retry shortly.", "type": "service_unavailable"}},
            headers={"Retry-After": "30"},
        )
    else:
        if is_inference:
            config_module.active_inference_requests += 1
            tracking_inference = True
        try:
            response = await call_next(request)
        except Exception:
            if tracking_inference:
                config_module.active_inference_requests -= 1
            raise

    if tracking_inference:
        body_iterator = response.body_iterator

        async def _tracked_body():
            try:
                async for chunk in body_iterator:
                    yield chunk
            finally:
                config_module.active_inference_requests -= 1

        response.body_iterator = _tracked_body()
    response.headers["X-Request-ID"] = request_id
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net "
        "https://unpkg.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https:; "
        "connect-src 'self' https:"
    )
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/") or request.url.path in ("/dashboard", "/login"):
        response.headers["Cache-Control"] = "no-store"
    return response

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(admin.router)
app.include_router(playground.router)
app.include_router(proxy.router)


if __name__ == "__main__":
    import uvicorn

    if PORT == 443:
        import http.server
        import socketserver
        import threading

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                host = self.headers.get('Host', ROUTER_DOMAIN)
                self.send_response(301)
                self.send_header('Location', f'https://{host}{self.path}')
                self.end_headers()

            def do_POST(self):
                self.do_GET()

            def do_HEAD(self):
                self.do_GET()

            def log_message(self, format, *args):
                pass

        def start_redirect_server():
            try:
                class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
                    allow_reuse_address = True
                server = ThreadedTCPServer(("0.0.0.0", 80), RedirectHandler)
                print("[LOG] Starting HTTP-to-HTTPS redirect server on port 80...")
                server.serve_forever()
            except Exception as e:
                print(f"[ERROR] Failed to start redirect server on port 80: {e}")

        threading.Thread(target=start_redirect_server, daemon=True).start()

    if os.path.exists(SSL_KEYFILE) and os.path.exists(SSL_CERTFILE):
        uvicorn.run(app, host="0.0.0.0", port=PORT, ssl_keyfile=SSL_KEYFILE, ssl_certfile=SSL_CERTFILE)
    else:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
