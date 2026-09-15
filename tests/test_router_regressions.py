import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import database
from app import config
from app.translator import stream_as_anthropic
from app.routers import proxy
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


class MobileDashboardPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        cls.dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")

    def test_mobile_disables_canvas_and_backdrop_compositing(self):
        self.assertIn("(pointer: coarse)", self.base)
        self.assertIn("#dot-canvas { display: none; }", self.base)
        self.assertIn("backdrop-filter: none !important", self.base)

    def test_live_updates_are_coalesced(self):
        self.assertIn("queueStatus(data.payload)", self.dashboard)
        self.assertIn("queueLiveLog(data.payload)", self.dashboard)
        self.assertIn("_pendingProviderKeys", self.dashboard)

    def test_log_rows_use_stable_keys(self):
        self.assertNotIn("log.timestamp + log.key_used + Math.random()", self.dashboard)

    def test_markdown_libraries_are_lazy_loaded(self):
        self.assertIn("ensureMarkdownLibraries()", self.dashboard)
        self.assertNotIn('<script defer src="https://cdn.jsdelivr.net/npm/marked', self.dashboard)


if __name__ == "__main__":
    unittest.main()
