import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from starlette.requests import Request

from app import database
from app import config
from app.translator import build_openai_request, stream_as_anthropic, to_anthropic_response
from app.translator_openai import openai_tool_choice_to_anthropic, openai_tools_to_anthropic
from app.routers import admin, proxy
from app.sse import SSEBroadcaster


ROOT = Path(__file__).resolve().parents[1]


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


class DatabaseHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_key_verification_uses_one_round_trip(self):
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
        self.assertIn("UPDATE router_api_keys", fetchrow.await_args.args[0])
        self.assertIn("RETURNING", fetchrow.await_args.args[0])

    async def test_session_cleanup_parameterizes_retention_days(self):
        with patch.object(database, "execute", AsyncMock()) as execute:
            await database.cleanup_old_sessions(14)

        query, days = execute.await_args.args
        self.assertIn("make_interval(days => $1)", query)
        self.assertEqual(days, 14)

    async def test_session_upsert_is_atomic_and_single_round_trip(self):
        with patch.object(database, "fetchrow", AsyncMock(return_value={"id": 42})) as fetchrow:
            session_id = await database.get_or_create_session("project", "hash", "model")

        self.assertEqual(session_id, 42)
        fetchrow.assert_awaited_once()
        query = fetchrow.await_args.args[0]
        self.assertIn("ON CONFLICT", query)
        self.assertIn("RETURNING id", query)


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
        with patch.object(config, "AUGMENT_SYSTEM_PROMPT", False):
            result = build_openai_request(body, provider="custom")

        self.assertEqual(result["max_tokens"], 32768)
        self.assertEqual(result["temperature"], 0.9)
        self.assertEqual(result["top_p"], 0.8)
        self.assertEqual(result["stop"], ["END"])
        self.assertEqual(result["tool_choice"], "none")
        self.assertTrue(result["tools"][0]["function"]["strict"])

    def test_anthropic_conversion_preserves_none_and_strict(self):
        self.assertEqual(openai_tool_choice_to_anthropic("none"), {"type": "none"})
        tools = openai_tools_to_anthropic([{
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}, "strict": True},
        }])
        self.assertTrue(tools[0]["strict"])

    def test_token_limit_is_reported_to_anthropic_clients(self):
        upstream = {
            "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }
        response = to_anthropic_response(upstream, "example", "msg_test")
        self.assertEqual(response["stop_reason"], "max_tokens")


class CustomProviderForwardingTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_builtin_openai_does_not_inject_brain_by_default(self):
        sent = []

        def handle(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={
                "model": "example", "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        body = {"model": "bm/example", "messages": [{"role": "user", "content": "Hi"}]}
        request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
        try:
            with (
                patch.object(proxy, "_check_router_auth", AsyncMock(return_value=True)),
                patch.object(proxy, "_read_json_payload", AsyncMock(return_value=(body, None))),
                patch.object(proxy, "_model_allowed_for_key", return_value=None),
                patch.object(proxy, "_key_model_prompt", return_value=""),
                patch.object(proxy, "BM_API_KEYS", ["secret"]),
                patch.object(proxy, "get_current_bm_key", return_value="secret"),
                patch.object(proxy, "_upstream_client", client),
                patch.object(proxy, "add_request_log"),
                patch.object(proxy, "_bill_router_key", AsyncMock()),
                patch.object(proxy.sse_broadcaster, "broadcast", AsyncMock()),
                patch.object(proxy, "_build_status_dict", AsyncMock(return_value={})),
                patch.object(database, "get_or_create_session", AsyncMock()) as session,
                patch.object(proxy.BrainMiddleware, "build_brain_context", AsyncMock()) as brain,
            ):
                response = await proxy.chat_completions(request)
        finally:
            await client.aclose()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent[0]["messages"], body["messages"])
        session.assert_not_awaited()
        brain.assert_not_awaited()


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
        for tab in ("keys", "playground", "brain", "models", "routing"):
            self.assertIn(f'<template x-if="activeTab === \'{tab}\'">', self.dashboard)
            self.assertNotIn(f'<div x-show="activeTab === \'{tab}\'"', self.dashboard)

    def test_live_logs_only_update_visible_unfiltered_activity(self):
        self.assertIn("if (document.hidden || this.activeTab !== 'activity' || this.logs.page !== 1 || this.logs.search", self.dashboard)
        self.assertIn("if (v === 'activity') this.fetchLogs();", self.dashboard)
        self.assertIn("if (Array.isArray(d.keys))", self.dashboard)


if __name__ == "__main__":
    unittest.main()
