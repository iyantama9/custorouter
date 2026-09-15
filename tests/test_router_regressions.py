import unittest
from unittest.mock import AsyncMock, patch

from app import database
from app.routers import proxy


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


if __name__ == "__main__":
    unittest.main()
