import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from app import database, redis_store
from app import config
from app.request_log_worker import _decode as decode_request_log_entries
from app.translator import build_openai_request, compact_messages, stream_as_anthropic, to_anthropic_response
from app.translator_openai import (
    anthropic_to_openai_response,
    make_anthropic_to_openai_stream_converter,
    openai_to_anthropic_messages,
    openai_tool_choice_to_anthropic,
    openai_tools_to_anthropic,
)
from app.routers import admin, playground, proxy
from app.sse import SSEBroadcaster


ROOT = Path(__file__).resolve().parents[1]


class AdminSecurityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _login_request(payload, ip):
        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        return Request({"type": "http", "method": "POST", "path": "/api/login", "headers": [],
                        "client": (ip, 12345)}, receive)

    def setUp(self):
        self.login_gate = patch.object(admin, "consume_login_attempt", AsyncMock(return_value=True))
        self.create_session = patch.object(admin, "create_session", AsyncMock())
        self.revoke_session = patch.object(admin, "revoke_session", AsyncMock())
        self.clear_attempts = patch.object(admin, "clear_login_attempts", AsyncMock())
        self.validate_session = patch.object(admin, "validate_session", AsyncMock(return_value=False))
        for mocked in (self.login_gate, self.create_session, self.revoke_session,
                       self.clear_attempts, self.validate_session):
            mocked.start()
        config.MIGRATION_DRAIN_ENABLED = False
        config.active_inference_requests = 0

    def tearDown(self):
        for mocked in (self.login_gate, self.create_session, self.revoke_session,
                       self.clear_attempts, self.validate_session):
            mocked.stop()
        config.MIGRATION_DRAIN_ENABLED = False
        config.active_inference_requests = 0

    async def test_login_issues_revocable_random_session_not_server_secret(self):
        request = self._login_request({"username": config.ADMIN_USERNAME, "password": "test"}, "198.51.100.8")
        with patch.object(admin, "verify_admin_password", return_value=True):
            response = await admin.api_login(request)
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"]
        token = cookie.split("session_token=", 1)[1].split(";", 1)[0]
        self.assertNotEqual(token, config.SESSION_SECRET)
        admin.create_session.assert_awaited_once_with(token, admin._SESSION_TTL_SECONDS)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        await admin.api_logout(token)
        admin.revoke_session.assert_awaited_once_with(token)

    async def test_login_rate_limit_reserves_attempts_before_password_check(self):
        admin.consume_login_attempt.side_effect = [True] * 5 + [False]
        with patch.object(admin, "verify_admin_password", return_value=False):
            responses = [await admin.api_login(self._login_request({"username": "wrong", "password": "wrong"},
                                                                      "198.51.100.9"))
                         for _ in range(6)]
        self.assertEqual([response.status_code for response in responses], [401] * 5 + [429])

    def test_only_configured_proxy_can_supply_client_ip(self):
        headers = [(b"x-real-ip", b"198.51.100.42"),
                   (b"x-forwarded-for", b"203.0.113.7, 198.51.100.42")]
        trusted = Request({"type": "http", "headers": headers, "client": ("172.18.0.1", 12345)})
        untrusted = Request({"type": "http", "headers": headers, "client": ("172.18.0.2", 12345)})
        with (patch.object(admin, "_TRUST_PROXY_HEADERS", True),
              patch.object(admin, "_TRUSTED_PROXY_IPS", {"172.18.0.1"})):
            self.assertEqual(admin._client_ip(trusted), "198.51.100.42")
            self.assertEqual(admin._client_ip(untrusted), "172.18.0.2")

    async def test_login_rejects_oversized_body_before_json_parsing(self):
        from app.main import app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://router.example") as client:
            response = await client.post("/api/login", content=b"x" * 5000,
                                         headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    async def test_unicode_username_is_rejected_without_server_error(self):
        request = self._login_request({"username": "用户", "password": "test"}, "198.51.100.10")
        with patch.object(admin, "verify_admin_password", return_value=True):
            response = await admin.api_login(request)
        self.assertEqual(response.status_code, 401)

    async def test_bcrypt_rejecting_long_password_does_not_crash_login(self):
        request = self._login_request({"username": config.ADMIN_USERNAME, "password": "x" * 100},
                                      "198.51.100.11")
        with patch.object(admin, "verify_admin_password", side_effect=ValueError("password too long")):
            response = await admin.api_login(request)
        self.assertEqual(response.status_code, 401)

    async def test_forwarded_host_cannot_bypass_admin_origin_check(self):
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://router.example",
                                     cookies={"session_token": "not-a-real-session"}) as client:
            response = await client.post(
                "/api/logout",
                headers={"Origin": "https://attacker.example", "X-Forwarded-Host": "attacker.example"},
            )
        self.assertEqual(response.status_code, 403)

    async def test_drain_endpoint_persists_only_boolean_state(self):
        with (
            patch.object(config, "set_migration_drain", AsyncMock(return_value={
                "draining": True, "active_inference_requests": 0,
            })) as set_drain,
            patch.object(admin.sse_broadcaster, "broadcast", AsyncMock()),
            patch.object(admin, "_build_status_dict", AsyncMock(return_value={})),
        ):
            response = await admin.set_migration_drain({"enabled": "true"})
            self.assertEqual(response.status_code, 400)
            result = await admin.set_migration_drain({"enabled": True})

        self.assertTrue(result["draining"])
        set_drain.assert_awaited_once_with(True)

    async def test_drain_rejects_new_inference_but_tracks_existing_stream(self):
        from app.main import security_and_observability_headers

        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
        config.MIGRATION_DRAIN_ENABLED = True
        rejected = await security_and_observability_headers(request, AsyncMock())
        self.assertEqual(rejected.status_code, 503)
        self.assertEqual(rejected.headers["retry-after"], "30")

        config.MIGRATION_DRAIN_ENABLED = False

        async def call_next(_request):
            async def stream():
                yield b"data: one\n\n"
            return StreamingResponse(stream(), media_type="text/event-stream")

        streamed = await security_and_observability_headers(request, call_next)
        self.assertEqual(config.active_inference_requests, 1)
        _ = [chunk async for chunk in streamed.body_iterator]
        self.assertEqual(config.active_inference_requests, 0)


class HttpClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await proxy.close_http_clients()

    async def test_upstream_client_is_reused(self):
        await proxy.init_http_clients()
        first = proxy._get_upstream_client()
        second = proxy._get_upstream_client()
        self.assertIs(first, second)
        self.assertFalse(first.is_closed)

    async def test_close_releases_both_pools(self):
        await proxy.init_http_clients()
        upstream = proxy._get_upstream_client()
        custom = proxy._get_custom_client()
        await proxy.close_http_clients()
        self.assertTrue(upstream.is_closed)
        self.assertTrue(custom.is_closed)


class PlaygroundPrivacyAndRoutingTests(unittest.TestCase):
    def test_builtin_provider_resolution_is_shared_for_qwen(self):
        self.assertEqual(proxy._builtin_provider_for_model("qc/qwen-plus"), "qc")
        self.assertEqual(proxy._builtin_provider_for_model("dh/claude"), "dahl")

    def test_playground_strips_reasoning_fields_and_tagged_thought(self):
        event = {
            "choices": [{"delta": {
                "reasoning_content": "private chain of thought",
                "content": "Answer <thinking>private thought</thinking> done",
            }}]
        }
        line = "data: " + json.dumps(event) + "\n"
        stripper = playground._ThinkingStripper()
        safe = playground._sanitize_openai_sse_line(line, stripper)
        parsed = json.loads(safe[0][6:])
        delta = parsed["choices"][0]["delta"]
        self.assertNotIn("reasoning_content", delta)
        # The trailing text remains in the stripper until the terminal event.
        done = playground._sanitize_openai_sse_line("data: [DONE]\n", stripper)
        visible = delta.get("content", "")
        if len(done) == 2:
            visible += json.loads(done[0][6:])["choices"][0]["delta"]["content"]
        self.assertEqual(visible, "Answer  done")

    def test_playground_thinking_stripper_handles_split_tags(self):
        stripper = playground._ThinkingStripper()
        self.assertEqual(stripper.feed("Visible <thi"), "Visible ")
        self.assertEqual(stripper.feed("nking>secret</think> final", final=True), " final")


class QwenCredentialRotationTests(unittest.TestCase):
    def setUp(self):
        self.original_keys = config.QC_API_KEYS[:]
        self.original_statuses = config.key_statuses.copy()
        self.original_indexes = config.qc_model_key_index.copy()
        config.QC_API_KEYS[:] = ["invalid-key", "valid-key"]
        config.key_statuses.clear()
        config.key_statuses.update({"invalid-key": "Active", "valid-key": "Standby"})
        config.qc_model_key_index.clear()

    def tearDown(self):
        config.QC_API_KEYS[:] = self.original_keys
        config.key_statuses.clear()
        config.key_statuses.update(self.original_statuses)
        config.qc_model_key_index.clear()
        config.qc_model_key_index.update(self.original_indexes)

    def test_invalid_qwen_key_is_skipped_for_future_requests(self):
        body = {"error": {"code": "invalid_api_key", "message": "Incorrect API key"}}
        def discard(coro):
            coro.close()

        with patch.object(config, "_bg", side_effect=discard):
            self.assertTrue(proxy._rotate_qc_after_failure("qwen-turbo", "invalid-key", 403, body))
        self.assertEqual(config.key_statuses["invalid-key"], "Invalid")
        self.assertEqual(config.get_current_qc_key_for_model("qwen-turbo"), "valid-key")


class DatabaseHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_key_verification_uses_one_read_round_trip(self):
        row = {
            "id": 7,
            "token_quota": 0,
            "tokens_used": 0,
            "allowed_models": "",
            "model_prompts": "{}",
            "model_aliases": "{}",
            "expires_at": None,
        }
        with patch.object(database, "fetchrow", AsyncMock(return_value=row)) as fetchrow:
            result = await database.verify_router_api_key("rtr_test")

        self.assertEqual(result["id"], 7)
        fetchrow.assert_awaited_once()
        self.assertIn("SELECT id", fetchrow.await_args.args[0])
        self.assertNotIn("UPDATE router_api_keys", fetchrow.await_args.args[0])

    async def test_router_key_last_used_touch_is_a_separate_coalescible_write(self):
        with patch.object(database, "execute", AsyncMock()) as execute:
            await database.touch_router_api_key_last_used(7)

        execute.assert_awaited_once()
        self.assertIn("UPDATE router_api_keys SET last_used_at", execute.await_args.args[0])

class SSEBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_client_queue_is_bounded_and_keeps_newest_event(self):
        broadcaster = SSEBroadcaster()
        queue = broadcaster.connect()
        for value in range(150):
            await broadcaster.broadcast("status", {"value": value})

        self.assertEqual(queue.qsize(), 100)
        newest = None
        while not queue.empty():
            newest = await queue.get()
        self.assertIn('"value": 149', newest)

    async def test_local_fanout_is_immediate_without_waiting_for_redis(self):
        broadcaster = SSEBroadcaster()
        queue = broadcaster.connect()

        await broadcaster.broadcast("log", {"id": "live-1"})

        self.assertIn('"id": "live-1"', await queue.get())


class LiveLogRedisTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_activity_page_uses_warm_redis_ring(self):
        original_total = config.total_requests
        config.total_requests = 42
        live_logs = [{"id": f"live-{idx}", "model": "wz/example"} for idx in range(15)]
        try:
            with (
                patch.object(admin, "get_live_logs", AsyncMock(return_value=live_logs)),
                patch.object(admin, "get_paginated_logs", AsyncMock()) as database_logs,
            ):
                result = await admin.api_logs(
                    user=None, page=1, per_page=15, search="",
                    sort_by="created_at", sort_order="DESC",
                )
        finally:
            config.total_requests = original_total

        self.assertEqual(result["logs"], live_logs)
        self.assertEqual(result["total"], 42)
        self.assertEqual(result["total_pages"], 3)
        database_logs.assert_not_awaited()


class RequestLogStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_log_enqueue_uses_bounded_redis_stream(self):
        client = AsyncMock()
        client.xadd.return_value = "1-0"
        with patch.object(redis_store, "_redis", return_value=client):
            entry_id = await redis_store.enqueue_request_log({"model": "wz/example"})

        self.assertEqual(entry_id, "1-0")
        self.assertEqual(client.xadd.await_args.args[0], "llm-router:request-logs:v1")
        self.assertEqual(client.xadd.await_args.kwargs["maxlen"], redis_store.REQUEST_LOG_STREAM_MAXLEN)
        self.assertTrue(client.xadd.await_args.kwargs["approximate"])

    def test_stream_entries_decode_to_idempotent_database_rows(self):
        rows, ids = decode_request_log_entries([("171-0", {"payload": json.dumps({
            "model": "wz/example", "status_code": 200, "key_prefix": "key...",
            "rotated": False, "latency_ms": 42, "input_tokens": 5,
            "output_tokens": 7, "cached_tokens": 0, "provider": "weize",
        })})])

        self.assertEqual(ids, ["171-0"])
        self.assertEqual(rows[0]["event_id"], "171-0")
        self.assertEqual(rows[0]["output_tokens"], 7)

    async def test_request_log_falls_back_to_postgres_when_redis_is_unavailable(self):
        payload = {
            "model": "wz/example", "status_code": 200, "key_prefix": "key...",
            "rotated": False, "latency_ms": 42, "input_tokens": 5,
            "output_tokens": 7, "cached_tokens": 0, "provider": "weize",
        }
        with (
            patch.object(config, "enqueue_request_log", AsyncMock(side_effect=RuntimeError("down"))),
            patch.object(config, "persist_request_log", AsyncMock()) as persist,
        ):
            await config._persist_request_log_off_path(payload)

        persist.assert_awaited_once_with(
            "wz/example", 200, "key...", False, 42, 5, 7, 0, "weize",
        )

    async def test_ack_keeps_stream_consumer_group_alive(self):
        client = AsyncMock()
        with patch.object(redis_store, "_redis", return_value=client):
            await redis_store.ack_request_log_entries(["1-0"])

        client.xack.assert_awaited_once_with(
            "llm-router:request-logs:v1", redis_store.REQUEST_LOG_CONSUMER_GROUP, "1-0",
        )
        self.assertFalse(hasattr(client, "xdel") and client.xdel.await_count)

    async def test_log_worker_heartbeat_is_expiring_and_health_checked(self):
        client = AsyncMock()
        client.exists.return_value = 1
        with patch.object(redis_store, "_redis", return_value=client):
            await redis_store.touch_request_log_worker_heartbeat()
            self.assertTrue(await redis_store.request_log_worker_healthy())

        client.set.assert_awaited_once()
        self.assertEqual(client.set.await_args.kwargs["ex"], 60)


class ModelRouteTests(unittest.IsolatedAsyncioTestCase):
    def _request_with_key(self, routes, allowed="wz/first,wz/second", body=None):
        if body is None:
            request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
        else:
            encoded = json.dumps(body).encode()
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": encoded, "more_body": False}

            request = Request(
                {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
                receive,
            )
        request.state.router_key = {
            "id": 1,
            "allowed_models": allowed,
            "model_routes": json.dumps(routes),
        }
        return request

    async def test_route_retries_candidates_and_keeps_public_route_name(self):
        request = self._request_with_key({"auto": ["wz/first", "wz/second"]})
        seen = []

        async def endpoint(candidate_request):
            body = await candidate_request.json()
            seen.append((body["model"], candidate_request.state.model_route_active))
            return JSONResponse(status_code=503 if body["model"] == "wz/first" else 200, content={})

        with (
            patch.object(proxy, "filter_healthy_model_route_candidates", AsyncMock(return_value=["wz/first", "wz/second"])),
            patch.object(proxy, "record_model_route_result", AsyncMock()),
        ):
            response = await proxy._run_model_route(
                endpoint, request, {"model": "auto", "messages": []},
                "auto", ["wz/first", "wz/second"],
            )

        self.assertEqual(seen, [("wz/first", True), ("wz/second", True)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-router-model-route"], "auto")
        self.assertEqual(response.headers["x-router-model-selected"], "wz/second")
        self.assertEqual(response.headers["x-router-model-attempt"], "2")

    async def test_route_skips_recently_failed_candidate_but_keeps_order(self):
        request = self._request_with_key({"auto": ["wz/first", "wz/second", "wz/third"]})
        seen = []

        async def endpoint(candidate_request):
            body = await candidate_request.json()
            seen.append(body["model"])
            return JSONResponse(status_code=200, content={})

        with (
            patch.object(proxy, "filter_healthy_model_route_candidates", AsyncMock(return_value=["wz/second", "wz/third"])),
            patch.object(proxy, "record_model_route_result", AsyncMock()) as record,
        ):
            response = await proxy._run_model_route(
                endpoint, request, {"model": "auto", "messages": []},
                "auto", ["wz/first", "wz/second", "wz/third"],
            )

        self.assertEqual(seen, ["wz/second"])
        self.assertEqual(response.headers["x-router-model-selected"], "wz/second")
        record.assert_awaited_once_with("wz/second", 200)

    async def test_route_retries_all_candidates_when_every_circuit_is_open(self):
        request = self._request_with_key({"auto": ["wz/first", "wz/second"]})
        seen = []

        async def endpoint(candidate_request):
            body = await candidate_request.json()
            seen.append(body["model"])
            return JSONResponse(status_code=200 if body["model"] == "wz/first" else 503, content={})

        with (
            patch.object(proxy, "filter_healthy_model_route_candidates", AsyncMock(return_value=[])),
            patch.object(proxy, "record_model_route_result", AsyncMock()),
        ):
            response = await proxy._run_model_route(
                endpoint, request, {"model": "auto", "messages": []},
                "auto", ["wz/first", "wz/second"],
            )

        self.assertEqual(seen, ["wz/first"])
        self.assertEqual(response.status_code, 200)

    def test_route_candidates_are_internal_even_if_legacy_allowlisted(self):
        request = self._request_with_key(
            {"auto": ["wz/first", "wz/second"]}, allowed="wz/direct"
        )
        self.assertIsNone(proxy._model_allowed_for_key(request, "auto"))
        self.assertIsNone(proxy._model_allowed_for_key(request, "wz/direct"))
        self.assertIn("reserved as a fallback", proxy._model_allowed_for_key(request, "wz/first"))
        self.assertIn("not allowed", proxy._model_allowed_for_key(request, "wz/other"))

        routed = proxy._clone_request_for_model_route(
            request, {"model": "wz/first", "messages": []}, "auto"
        )
        self.assertIsNone(proxy._model_allowed_for_key(routed, "wz/first"))

    def test_route_reader_keeps_up_to_twenty_four_candidates(self):
        candidates = [f"test/{index}" for index in range(25)]
        request = self._request_with_key({"auto": candidates}, allowed=",".join(candidates))
        self.assertEqual(proxy._key_model_routes(request)["auto"], candidates[:24])

    async def test_model_catalog_exposes_route_but_hides_its_candidates(self):
        request = self._request_with_key(
            {"auto": ["test/first", "test/second"]},
            allowed="test/direct",
        )
        cache_enabled = proxy._model_catalog_cache_enabled
        proxy._model_catalog_cache_enabled = False
        try:
            with (
                patch.object(proxy, "_check_router_auth", AsyncMock(return_value=True)),
                patch.dict(config.CUSTOM_PROVIDERS, {"test": {"models": ["first", "second"]}}, clear=True),
                patch.object(config, "DISABLED_PROVIDERS", {"bm", "nry", "dahl", "qc", "marketku"}),
            ):
                response = await proxy.list_models(request)
        finally:
            proxy._model_catalog_cache_enabled = cache_enabled

        ids = [model["id"] for model in json.loads(response.body)["data"]]
        self.assertEqual(ids, ["auto"])

    def test_route_settings_keep_direct_and_route_models_separate(self):
        settings = admin._parse_key_settings({
            "allowed_models": ["wz/direct"],
            "model_routes": {"auto": ["wz/fallback"]},
        })
        self.assertEqual(settings["allowed_models"], "wz/direct")
        self.assertEqual(json.loads(settings["model_routes"]), {"auto": ["wz/fallback"]})
        with self.assertRaises(admin._KeySettingsError):
            admin._parse_key_settings({
                "model_routes": {"auto": ["backup"], "backup": ["wz/first"]},
            })

    async def test_openai_endpoint_retries_route_and_echoes_auto(self):
        body = {"model": "auto", "messages": [{"role": "user", "content": "Hi"}]}
        request = self._request_with_key(
            {"auto": ["test/first", "test/second"]},
            allowed="test/direct", body=body,
        )
        attempts = [
            ("json", 503, {"error": {"message": "first unavailable"}}, "key-1"),
            ("json", 200, {"model": "second", "choices": [], "usage": {}}, "key-2"),
        ]
        with (
            patch.dict(config.CUSTOM_PROVIDERS, {"test": {"api_format": "openai"}}),
            patch.object(proxy, "_dispatch_custom_openai", AsyncMock(side_effect=attempts)) as dispatch,
            patch.object(proxy, "add_request_log"),
            patch.object(proxy, "_bill_router_key", AsyncMock()),
            patch.object(proxy, "_broadcast_request_log", AsyncMock()),
        ):
            response = await proxy.chat_completions(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["model"], "auto")
        self.assertEqual([call.args[1]["model"] for call in dispatch.await_args_list], ["first", "second"])
        self.assertEqual(response.headers["x-router-model-selected"], "test/second")

    async def test_anthropic_endpoint_retries_route_and_echoes_auto(self):
        body = {"model": "auto", "messages": [{"role": "user", "content": "Hi"}]}
        request = self._request_with_key(
            {"auto": ["test/first", "test/second"]},
            allowed="test/direct", body=body,
        )
        attempts = [
            ("json", 503, {"error": {"message": "first unavailable"}}),
            ("json", 200, {"model": "second", "content": [], "usage": {}}),
        ]
        with (
            patch.dict(config.CUSTOM_PROVIDERS, {"test": {"api_format": "anthropic"}}),
            patch.object(proxy, "_dispatch_custom_provider", AsyncMock(side_effect=attempts)) as dispatch,
            patch.object(proxy, "add_request_log"),
            patch.object(proxy, "_bill_router_key", AsyncMock()),
            patch.object(proxy, "_broadcast_request_log", AsyncMock()),
        ):
            response = await proxy.messages(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["model"], "auto")
        self.assertEqual([call.args[1]["model"] for call in dispatch.await_args_list], ["first", "second"])
        self.assertEqual(response.headers["x-router-model-selected"], "test/second")


class AnthropicStreamTranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_openai_chunks_with_null_usage(self):
        chunks = [
            {
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }],
                "usage": None,
            },
            {
                "choices": [{
                    "index": 0,
                    "delta": {"content": "OK"},
                    "finish_reason": None,
                }],
                "usage": None,
            },
            {
                "choices": [{
                    "index": 0,
                    "delta": {"content": ""},
                    "finish_reason": "stop",
                }],
                "usage": None,
            },
            {"choices": [], "usage": {"completion_tokens": 1}},
        ]

        class FakeResponse:
            async def aiter_lines(self):
                for chunk in chunks:
                    yield f"data: {json.dumps(chunk)}"
                yield "data: [DONE]"

        events = [
            event
            async for event in stream_as_anthropic(
                FakeResponse(), "qc/glm-5.3", "msg_test"
            )
        ]

        output = "".join(events)
        self.assertIn('"type": "content_block_delta"', output)
        self.assertIn('"text": "OK"', output)
        self.assertIn('"type": "message_stop"', output)


class QwenPerModelRotationTests(unittest.TestCase):
    def setUp(self):
        self.original_keys = list(config.QC_API_KEYS)
        self.original_indexes = dict(config.qc_model_key_index)
        self.original_failures = {
            key: dict(models) for key, models in config.qc_model_failures.items()
        }
        self.original_failover_count = config.failover_count
        config.QC_API_KEYS[:] = ["key-1", "key-2", "key-3"]
        config.qc_model_key_index.clear()
        config.qc_model_failures.clear()
        self.bg_patcher = patch.object(config, "_bg", side_effect=lambda coro: coro.close())
        self.bg_patcher.start()

    def tearDown(self):
        self.bg_patcher.stop()
        config.QC_API_KEYS[:] = self.original_keys
        config.qc_model_key_index.clear()
        config.qc_model_key_index.update(self.original_indexes)
        config.qc_model_failures.clear()
        config.qc_model_failures.update(self.original_failures)
        config.failover_count = self.original_failover_count

    def test_each_model_starts_from_first_available_key(self):
        config.mark_qc_model_exhausted("key-1", "glm-5.3")
        self.assertEqual(config.get_current_qc_key_for_model("glm-5.3"), "key-2")
        self.assertEqual(config.get_current_qc_key_for_model("qwen3.5-plus"), "key-1")

    def test_rotation_is_relative_to_the_key_that_failed(self):
        config.mark_qc_model_exhausted("key-1", "glm-5.3")
        self.assertTrue(config.rotate_qc_key_for_model("glm-5.3", after_key="key-1"))
        self.assertEqual(config.get_current_qc_key_for_model("glm-5.3"), "key-2")

        # A concurrent failure from key-1 must not advance the shared cursor
        # past key-2 merely because another request already rotated it.
        self.assertTrue(config.rotate_qc_key_for_model("glm-5.3", after_key="key-1"))
        self.assertEqual(config.get_current_qc_key_for_model("glm-5.3"), "key-2")

    def test_returns_no_key_when_model_is_exhausted_everywhere(self):
        for key in config.QC_API_KEYS:
            config.mark_qc_model_exhausted(key, "glm-5.3")
        self.assertEqual(config.get_current_qc_key_for_model("glm-5.3"), "")
        self.assertFalse(config.rotate_qc_key_for_model("glm-5.3", after_key="key-3"))

    def test_quota_failure_only_exhausts_the_requested_model(self):
        self.assertTrue(proxy._rotate_qc_after_failure(
            "glm-5.3", "key-1", 429, {"error": {"message": "quota exhausted"}}
        ))
        self.assertTrue(config.is_qc_model_exhausted("key-1", "glm-5.3"))
        self.assertFalse(config.is_qc_model_exhausted("key-1", "qwen3.5-plus"))

    def test_transient_failure_rotates_without_marking_quota_exhausted(self):
        self.assertTrue(proxy._rotate_qc_after_failure(
            "glm-5.3", "key-1", 500, {"error": {"message": "temporary failure"}}
        ))
        self.assertFalse(config.is_qc_model_exhausted("key-1", "glm-5.3"))


class OpenAIStreamRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_null_usage_stream_is_relayed_and_closed(self):
        class FakeResponse:
            def __init__(self):
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"model":"glm-5.3","choices":[{"delta":{"content":"OK"}}],"usage":null}\n\n'
                yield b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
                yield b'data: [DONE]\n\n'

            async def aclose(self):
                self.closed = True

        response = FakeResponse()
        request = object()
        with (
            patch.object(proxy, "add_request_log") as add_log,
            patch.object(proxy, "_bill_router_key", AsyncMock()) as bill,
            patch.object(proxy, "_broadcast_request_log", AsyncMock()) as broadcast,
        ):
            chunks = [
                chunk async for chunk in proxy._relay_openai_upstream_stream(
                    response,
                    requested_model="qc/glm-5.3",
                    display_model="qc/glm-5.3",
                    current_key="key-1",
                    provider="qc",
                    started_at=0,
                    request=request,
                )
            ]

        output = b"".join(chunks)
        self.assertIn(b'"content": "OK"', output)
        self.assertTrue(response.closed)
        add_log.assert_called_once()
        bill.assert_awaited_once_with(request, 3)
        broadcast.assert_awaited_once()


class ProviderTransparencyTests(unittest.TestCase):
    def test_third_party_headers_do_not_forward_router_credential(self):
        info = {
            "api_format": "anthropic", "auth_header": "x-api-key",
            "anthropic_version": "",
        }
        headers = config.custom_provider_headers(
            info, "upstream-secret", {
                "authorization": "Bearer router-secret",
                "x-api-key": "router-secret",
                "anthropic-beta": "feature-1",
            },
        )
        self.assertEqual(headers["x-api-key"], "upstream-secret")
        self.assertEqual(headers["anthropic-beta"], "feature-1")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("anthropic-version", headers)

    def test_translation_preserves_explicit_sampling_and_token_limits(self):
        body = {
            "model": "example",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 32768,
            "temperature": 0.9,
            "top_p": 0.8,
            "stop_sequences": ["END"],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}, "strict": True}],
            "tool_choice": {"type": "none"},
        }
        result = build_openai_request(body, provider="custom")

        self.assertEqual(result["max_tokens"], 32768)
        self.assertEqual(result["temperature"], 0.9)
        self.assertEqual(result["top_p"], 0.8)
        self.assertEqual(result["stop"], ["END"])
        self.assertEqual(result["tool_choice"], "none")
        self.assertTrue(result["tools"][0]["function"]["strict"])

    def test_anthropic_parallel_tool_constraint_reaches_openai(self):
        body = {
            "model": "example",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "lookup", "disable_parallel_tool_use": True},
        }
        result = build_openai_request(body, provider="custom")

        self.assertEqual(result["tool_choice"], {"type": "function", "function": {"name": "lookup"}})
        self.assertFalse(result["parallel_tool_calls"])

    def test_router_never_injects_a_global_behavior_prompt(self):
        body = {
            "model": "example",
            "messages": [{"role": "user", "content": "Answer in exactly one sentence."}],
        }
        # A legacy environment variable must no longer be able to add a
        # router-authored system prompt or alter the model's behavior.
        with patch.object(config, "AUGMENT_SYSTEM_PROMPT", True, create=True):
            result = build_openai_request(body, provider="custom")

        self.assertEqual(result["messages"], body["messages"])

    def test_qwen_vision_input_is_not_reduced_to_a_text_placeholder(self):
        body = {
            "model": "qwen3-vl-plus",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this image."},
                {"type": "image", "source": {"type": "url", "url": "https://example.test/image.jpg"}},
            ]}],
        }
        result = build_openai_request(body, provider="qc")
        content = result["messages"][0]["content"]

        self.assertIsInstance(content, list)
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["url"], "https://example.test/image.jpg")

    def test_context_compaction_does_not_add_a_router_authored_system_message(self):
        messages = [
            {"role": "system", "content": "Use the caller's style."},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "new reply"},
        ]
        compacted = compact_messages(messages, keep_last=2)

        self.assertEqual(compacted, [
            {"role": "system", "content": "Use the caller's style."},
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "new reply"},
        ])

    def test_anthropic_conversion_preserves_none_and_strict(self):
        self.assertEqual(openai_tool_choice_to_anthropic("none"), {"type": "none"})
        tools = openai_tools_to_anthropic([{
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}, "strict": True},
        }])
        self.assertTrue(tools[0]["strict"])

    def test_openai_parallel_tool_results_become_one_anthropic_turn(self):
        _, messages = openai_to_anthropic_messages([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_weather", "type": "function", "function": {"name": "weather", "arguments": "{}"}},
                {"id": "call_time", "type": "function", "function": {"name": "time", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_weather", "content": "Sunny"},
            {"role": "tool", "tool_call_id": "call_time", "content": "10:00"},
        ])

        self.assertEqual([message["role"] for message in messages], ["assistant", "user"])
        self.assertEqual([block["tool_use_id"] for block in messages[1]["content"]], ["call_weather", "call_time"])

    def test_anthropic_tool_response_and_stream_are_exposed_as_openai_calls(self):
        response = anthropic_to_openai_response({
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}}],
            "stop_reason": "tool_use",
        }, "test/model")
        choice = response["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["arguments"], '{"q": "x"}')

        convert = make_anthropic_to_openai_stream_converter("test/model")
        start = json.loads(convert({
            "type": "content_block_start", "index": 2,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "lookup"},
        }).split("data: ", 1)[1])
        args = json.loads(convert({
            "type": "content_block_delta", "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'},
        }).split("data: ", 1)[1])
        finish = json.loads(convert({
            "type": "message_delta", "delta": {"stop_reason": "tool_use"},
        }).split("data: ", 1)[1])
        self.assertEqual(start["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(args["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], '{"q":"x"}')
        self.assertEqual(finish["choices"][0]["finish_reason"], "tool_calls")

    def test_token_limit_is_reported_to_anthropic_clients(self):
        upstream = {
            "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }
        response = to_anthropic_response(upstream, "example", "msg_test")
        self.assertEqual(response["stop_reason"], "max_tokens")


class CustomProviderForwardingTests(unittest.IsolatedAsyncioTestCase):
    def test_quota_wrapped_in_400_retries_but_bad_payload_does_not(self):
        self.assertTrue(proxy._should_retry_custom_key(400, {
            "error": {"code": "out_of_credit", "message": "Out of credit"}
        }))
        self.assertFalse(proxy._should_retry_custom_key(400, {
            "error": {"message": "Invalid messages payload"}
        }))

    async def test_custom_openai_quota_400_tries_every_key_until_success(self):
        seen = []

        def handle(request):
            seen.append(request.headers["authorization"])
            if len(seen) < 3:
                return httpx.Response(400, json={"error": {"code": "out_of_credit"}})
            return httpx.Response(200, json={"choices": [], "usage": {}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        retry_state = {}
        try:
            with (
                patch.dict(config.CUSTOM_PROVIDERS, {"bi": {"base_url": "https://provider.test/v1", "api_format": "openai"}}),
                patch.dict(config.CUSTOM_PROVIDER_KEYS, {"bi": ["key-1", "key-2", "key-3"]}),
                patch.object(config, "get_current_custom_key", return_value="key-1"),
                patch.object(config, "rotate_custom_key") as rotate,
                patch.object(proxy, "_custom_client", client),
            ):
                kind, status, _, key = await proxy._dispatch_custom_openai(
                    "bi", {"model": "example", "messages": []}, False, retry_state=retry_state
                )
        finally:
            await client.aclose()

        self.assertEqual((kind, status, key), ("json", 200, "key-3"))
        self.assertEqual(seen, ["Bearer key-1", "Bearer key-2", "Bearer key-3"])
        self.assertEqual(rotate.call_count, 2)
        self.assertTrue(retry_state["rotated"])

    async def test_custom_openai_validation_400_does_not_rotate(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": {"message": "Invalid payload"}})
        ))
        try:
            with (
                patch.dict(config.CUSTOM_PROVIDERS, {"bi": {"base_url": "https://provider.test/v1", "api_format": "openai"}}),
                patch.dict(config.CUSTOM_PROVIDER_KEYS, {"bi": ["key-1", "key-2"]}),
                patch.object(config, "get_current_custom_key", return_value="key-1"),
                patch.object(config, "rotate_custom_key") as rotate,
                patch.object(proxy, "_custom_client", client),
            ):
                _, status, _, _ = await proxy._dispatch_custom_openai(
                    "bi", {"model": "example", "messages": []}, False
                )
        finally:
            await client.aclose()
        self.assertEqual(status, 400)
        rotate.assert_not_called()

    async def test_custom_openai_stream_quota_tries_all_keys(self):
        seen = []

        def handle(request):
            seen.append(request.headers["authorization"])
            if len(seen) < 3:
                return httpx.Response(400, json={"error": {"message": "Out of credit"}})
            return httpx.Response(200, text="data: [DONE]\n\n")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        retry_state = {}
        try:
            with (
                patch.dict(config.CUSTOM_PROVIDERS, {"bi": {"base_url": "https://provider.test/v1", "api_format": "openai"}}),
                patch.dict(config.CUSTOM_PROVIDER_KEYS, {"bi": ["key-1", "key-2", "key-3"]}),
                patch.object(config, "get_current_custom_key", return_value="key-2"),
                patch.object(config, "rotate_custom_key") as rotate,
                patch.object(proxy, "_custom_client", client),
            ):
                kind, status, response, key = await proxy._dispatch_custom_openai(
                    "bi", {"model": "example", "messages": [], "stream": True}, True,
                    retry_state=retry_state,
                )
                await response.aclose()
        finally:
            await client.aclose()

        self.assertEqual((kind, status, key), ("stream", 200, "key-1"))
        self.assertEqual(seen, ["Bearer key-2", "Bearer key-3", "Bearer key-1"])
        self.assertEqual(rotate.call_count, 2)
        self.assertTrue(retry_state["rotated"])

    async def test_custom_anthropic_quota_tries_all_keys_before_failure(self):
        seen = []

        def handle(request):
            seen.append(request.headers["x-api-key"])
            return httpx.Response(400, json={"error": {"message": "Insufficient balance"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        retry_state = {}
        try:
            with (
                patch.dict(config.CUSTOM_PROVIDERS, {"third": {"base_url": "https://provider.test/v1", "api_format": "anthropic", "auth_header": "x-api-key"}}),
                patch.dict(config.CUSTOM_PROVIDER_KEYS, {"third": ["key-1", "key-2", "key-3"]}),
                patch.object(config, "get_current_custom_key", return_value="key-1"),
                patch.object(config, "rotate_custom_key") as rotate,
                patch.object(proxy, "_custom_client", client),
            ):
                kind, status, _ = await proxy._dispatch_custom_provider(
                    "third", {"model": "example", "messages": [], "max_tokens": 10}, False,
                    retry_state=retry_state,
                )
        finally:
            await client.aclose()

        self.assertEqual((kind, status), ("json", 400))
        self.assertEqual(seen, ["key-1", "key-2", "key-3"])
        self.assertEqual(rotate.call_count, 2)
        self.assertTrue(retry_state["rotated"])

    async def test_third_party_model_probe_uses_configured_auth(self):
        sent = []

        def handle(request):
            sent.append(request)
            return httpx.Response(200, json={"data": []})

        info = {
            "base_url": "https://provider.test/v1", "api_format": "anthropic",
            "auth_header": "x-api-key", "anthropic_version": "",
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await admin._probe_provider(client, "third", info, "upstream-secret")

        self.assertEqual(result, "third")
        self.assertEqual(sent[0].headers["x-api-key"], "upstream-secret")
        self.assertNotIn("authorization", sent[0].headers)
        self.assertNotIn("anthropic-version", sent[0].headers)

    async def test_live_status_omits_large_key_and_log_snapshots(self):
        with (
            patch.object(config, "get_masked_keys", side_effect=AssertionError("key rows should not be built")),
            patch.object(config, "providers_signature", side_effect=AssertionError("catalog should not be hashed")),
        ):
            status = await proxy._build_status_dict(include_details=False)
        self.assertIn("total_requests", status)
        self.assertNotIn("keys", status)
        self.assertNotIn("recent_requests", status)
        self.assertNotIn("providers_signature", status)

    async def test_custom_openai_forwards_payload_without_rewriting(self):
        sent = []

        def handle(request):
            sent.append(request)
            return httpx.Response(200, json={"model": "example", "choices": [], "usage": {}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        payload = {
            "model": "example", "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.9, "max_tokens": 32768, "tool_choice": "none",
            "tools": [{"type": "function", "function": {"name": "lookup", "strict": True}}],
            "response_format": {"type": "json_object"},
        }
        try:
            with (
                patch.dict(config.CUSTOM_PROVIDERS, {"test": {"base_url": "https://provider.test/v1", "api_format": "openai"}}),
                patch.dict(config.CUSTOM_PROVIDER_KEYS, {"test": ["secret"]}),
                patch.object(config, "get_current_custom_key", return_value="secret"),
                patch.object(proxy, "_custom_client", client),
            ):
                kind, status, body, key = await proxy._dispatch_custom_openai("test", payload, False)
        finally:
            await client.aclose()

        self.assertEqual((kind, status, key), ("json", 200, "secret"))
        self.assertEqual(body["model"], "example")
        self.assertEqual(json.loads(sent[0].content), payload)
        self.assertEqual(sent[0].headers["authorization"], "Bearer secret")

    async def test_custom_openai_stream_keeps_tool_deltas_and_parameters(self):
        sent = []

        def handle(request):
            sent.append(request)
            event = {
                "model": "example",
                "choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }]}}],
                "usage": None,
            }
            return httpx.Response(200, text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        payload = {
            "model": "example", "messages": [{"role": "user", "content": "Hi"}],
            "stream": True, "temperature": 1.0, "max_tokens": 65536,
            "tools": [{"type": "function", "function": {"name": "lookup", "strict": True}}],
        }
        try:
            with (
                patch.dict(config.CUSTOM_PROVIDERS, {"test": {"base_url": "https://provider.test/v1", "api_format": "openai"}}),
                patch.dict(config.CUSTOM_PROVIDER_KEYS, {"test": ["secret"]}),
                patch.object(config, "get_current_custom_key", return_value="secret"),
                patch.object(proxy, "_custom_client", client),
            ):
                kind, status, response, key = await proxy._dispatch_custom_openai("test", payload, True)
                with (
                    patch.object(proxy, "add_request_log"),
                    patch.object(proxy, "_bill_router_key", AsyncMock()) as bill,
                    patch.object(proxy, "_broadcast_request_log", AsyncMock()),
                ):
                    chunks = [chunk async for chunk in proxy._relay_openai_upstream_stream(
                        response,
                        requested_model="test/example",
                        display_model="test/example",
                        current_key=key,
                        provider="test",
                        started_at=0,
                        request=object(),
                        strip_thinking=False,
                        input_tokens_estimate=5,
                    )]
        finally:
            await client.aclose()

        self.assertEqual((kind, status), ("stream", 200))
        self.assertEqual(json.loads(sent[0].content), payload)
        self.assertIn(b'"tool_calls"', b"".join(chunks))
        self.assertEqual(bill.await_args.args[1] > 5, True)

    async def test_custom_anthropic_includes_required_version_header(self):
        sent = []

        def handle(request):
            sent.append(request)
            return httpx.Response(200, json={"model": "claude", "content": [], "usage": {}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        payload = {"model": "claude", "messages": [], "max_tokens": 32768}
        try:
            with (
                patch.dict(config.CUSTOM_PROVIDERS, {"test": {"base_url": "https://api.anthropic.com/v1", "api_format": "anthropic"}}),
                patch.dict(config.CUSTOM_PROVIDER_KEYS, {"test": ["secret"]}),
                patch.object(config, "get_current_custom_key", return_value="secret"),
                patch.object(proxy, "_custom_client", client),
            ):
                kind, status, _ = await proxy._dispatch_custom_provider(
                    "test", payload, False, anthropic_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
                )
        finally:
            await client.aclose()

        self.assertEqual((kind, status), ("json", 200))
        self.assertEqual(sent[0].headers["anthropic-version"], "2023-06-01")
        self.assertEqual(sent[0].headers["anthropic-beta"], "prompt-caching-2024-07-31")
        self.assertEqual(json.loads(sent[0].content), payload)

    async def test_openai_route_uses_direct_custom_path(self):
        body = {
            "model": "test/example", "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.9, "max_tokens": 32768,
            "tool_choice": "none", "response_format": {"type": "json_object"},
        }
        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
        upstream = {"model": "example", "choices": [], "usage": {}}
        with (
            patch.dict(config.CUSTOM_PROVIDERS, {"test": {"api_format": "openai"}}),
            patch.object(proxy, "_check_router_auth", AsyncMock(return_value=True)),
            patch.object(proxy, "_read_json_payload", AsyncMock(return_value=(body, None))),
            patch.object(proxy, "_model_allowed_for_key", return_value=None),
            patch.object(proxy, "_key_model_prompt", return_value=""),
            patch.object(proxy, "_dispatch_custom_openai", AsyncMock(return_value=("json", 200, upstream, "secret"))) as dispatch,
            patch.object(proxy, "add_request_log"),
            patch.object(proxy, "_bill_router_key", AsyncMock()),
            patch.object(proxy, "_broadcast_request_log", AsyncMock()),
        ):
            response = await proxy.chat_completions(request)

        sent = dispatch.await_args.args[1]
        self.assertEqual(sent, {**body, "model": "example"})
        self.assertEqual(response.status_code, 200)

    async def test_openai_route_logs_custom_key_rotation(self):
        body = {"model": "test/example", "messages": [{"role": "user", "content": "Hi"}]}
        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})

        async def dispatch(_prefix, _payload, _stream, *, retry_state):
            retry_state["rotated"] = True
            return "json", 200, {"model": "example", "choices": [], "usage": {}}, "key-2"

        with (
            patch.dict(config.CUSTOM_PROVIDERS, {"test": {"api_format": "openai"}}),
            patch.object(proxy, "_check_router_auth", AsyncMock(return_value=True)),
            patch.object(proxy, "_read_json_payload", AsyncMock(return_value=(body, None))),
            patch.object(proxy, "_model_allowed_for_key", return_value=None),
            patch.object(proxy, "_key_model_prompt", return_value=""),
            patch.object(proxy, "_dispatch_custom_openai", side_effect=dispatch),
            patch.object(proxy, "add_request_log") as log,
            patch.object(proxy, "_bill_router_key", AsyncMock()),
            patch.object(proxy, "_broadcast_request_log", AsyncMock()),
        ):
            response = await proxy.chat_completions(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(log.call_args.args[3])

    async def test_openai_to_anthropic_keeps_sampling_limits_and_tool_policy(self):
        body = {
            "model": "test/claude", "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.8, "max_tokens": 32768,
            "tool_choice": "none", "parallel_tool_calls": False,
            "tools": [{"type": "function", "function": {
                "name": "lookup", "parameters": {"type": "object"}, "strict": True,
            }}],
        }
        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
        upstream = {"model": "claude", "content": [], "usage": {}}
        with (
            patch.dict(config.CUSTOM_PROVIDERS, {"test": {"api_format": "anthropic"}}),
            patch.object(proxy, "_check_router_auth", AsyncMock(return_value=True)),
            patch.object(proxy, "_read_json_payload", AsyncMock(return_value=(body, None))),
            patch.object(proxy, "_model_allowed_for_key", return_value=None),
            patch.object(proxy, "_key_model_prompt", return_value=""),
            patch.object(proxy, "_dispatch_custom_provider", AsyncMock(return_value=("json", 200, upstream))) as dispatch,
            patch.object(proxy, "add_request_log"),
            patch.object(proxy, "_bill_router_key", AsyncMock()),
            patch.object(proxy, "_broadcast_request_log", AsyncMock()),
        ):
            response = await proxy.chat_completions(request)

        sent = dispatch.await_args.args[1]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent["temperature"], 0.8)
        self.assertEqual(sent["max_tokens"], 32768)
        self.assertEqual(sent["tool_choice"], {"type": "none"})
        self.assertTrue(sent["tools"][0]["strict"])

class MobileDashboardPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        cls.dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")

    def test_mobile_disables_canvas_and_backdrop_compositing(self):
        self.assertIn("(pointer: coarse)", self.base)
        self.assertIn("#dot-canvas { display: none; }", self.base)
        self.assertIn("backdrop-filter: none !important", self.base)
        self.assertIn("document.querySelector('[x-data=\"dashboard()\"]')", self.base)

    def test_wide_dashboard_tables_are_contained_on_mobile(self):
        self.assertIn(".dashboard-shell { overflow-x: hidden; }", self.dashboard)
        self.assertIn(".dashboard-table-scroll", self.dashboard)
        self.assertIn("overscroll-behavior-x: contain", self.dashboard)
        self.assertIn("sm:flex-row", self.dashboard)
        self.assertIn("dashboard-table--providers", self.dashboard)

    def test_model_routes_use_catalog_picker_not_free_text_candidates(self):
        self.assertIn("openRouteModelPicker(index)", self.dashboard)
        self.assertIn("routePickerModels()", self.dashboard)
        self.assertIn("toggleRouteModel(m.id)", self.dashboard)
        self.assertIn("moveRouteTarget(index, targetIndex", self.dashboard)
        self.assertIn(".route-model-picker-layer { z-index: 110; }", self.dashboard)
        self.assertIn("Route models stay separate from Direct Models", self.dashboard)
        self.assertIn("const routed = new Set(this.routerKeys.modelRoutes.flatMap", self.dashboard)
        self.assertNotIn('placeholder="wz/model-a, qc/model-b, nn/model-c"', self.dashboard)

    def test_live_updates_are_coalesced(self):
        self.assertIn("queueStatus(data.payload)", self.dashboard)
        self.assertIn("queueLiveLog(data.payload)", self.dashboard)
        self.assertIn("_pendingProviderKeys", self.dashboard)

    def test_log_rows_use_stable_keys(self):
        self.assertNotIn("log.timestamp + log.key_used + Math.random()", self.dashboard)

    def test_markdown_libraries_are_lazy_loaded(self):
        self.assertIn("ensureMarkdownLibraries()", self.dashboard)
        self.assertNotIn('<script defer src="https://cdn.jsdelivr.net/npm/marked', self.dashboard)

    def test_inactive_tabs_are_not_hydrated_on_initial_load(self):
        for tab in ("keys", "playground", "models", "routing"):
            self.assertIn(f'<template x-if="activeTab === \'{tab}\'">', self.dashboard)
            self.assertNotIn(f'<div x-show="activeTab === \'{tab}\'"', self.dashboard)

    def test_live_logs_only_update_visible_unfiltered_activity(self):
        self.assertIn("if (document.hidden || this.activeTab !== 'activity' || this.logs.page !== 1 || this.logs.search", self.dashboard)
        self.assertIn("if (v === 'activity') this.fetchLogs();", self.dashboard)
        self.assertIn("if (Array.isArray(d.keys))", self.dashboard)


if __name__ == "__main__":
    unittest.main()
