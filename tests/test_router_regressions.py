import json
import unittest
from unittest.mock import AsyncMock, patch

from app import database
from app.translator import stream_as_anthropic
from app.routers import proxy
from app.sse import SSEBroadcaster


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


if __name__ == "__main__":
    unittest.main()
