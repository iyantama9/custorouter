import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DB_POOL_MIN_SIZE = max(1, int(os.getenv("DB_POOL_MIN_SIZE", "2")))
DB_POOL_MAX_SIZE = max(DB_POOL_MIN_SIZE, int(os.getenv("DB_POOL_MAX_SIZE", "10")))
DB_COMMAND_TIMEOUT = max(1.0, float(os.getenv("DB_COMMAND_TIMEOUT", "30")))

_pool = None

async def init_db():
    import asyncpg
    global _pool
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set")
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
        command_timeout=DB_COMMAND_TIMEOUT,
    )
    await setup_tables()

async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

async def execute(query, *args):
    if not _pool:
        return
    async with _pool.acquire() as conn:
        return await conn.execute(query, *args)

async def fetch(query, *args):
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        return await conn.fetch(query, *args)

async def fetchrow(query, *args):
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        return await conn.fetchrow(query, *args)

async def fetch_one(query, *args):
    return await fetchrow(query, *args)


async def persist_request_log(
    model, status_code, key_prefix, rotated, latency_ms,
    input_tokens, output_tokens, cached_tokens, provider,
):
    """Persist one request log without adding counter writes to the hot path."""
    await execute(
        """
        INSERT INTO request_logs
            (model, status_code, key_prefix, rotated, latency_ms,
             input_tokens, output_tokens, cached_tokens, provider)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        model, status_code, key_prefix, rotated, latency_ms,
        input_tokens, output_tokens, cached_tokens, provider,
    )

async def get_lifetime_stats():
    """All-time request/token/rotation totals, computed straight from
    request_logs rather than the separately-tracked in-memory counters in
    config.py. Those counters reset to 0 on every process restart and were
    never restored from their own persisted snapshot at startup, so the
    dashboard cards looked "reset" after every deploy even though the log
    table itself never lost anything. Querying the ground truth directly
    means there's nothing left to fall out of sync.
    """
    row = await fetchrow("""
        SELECT
            COUNT(*)                                          AS total_requests,
            COALESCE(SUM(input_tokens + output_tokens), 0)    AS total_tokens,
            COUNT(*) FILTER (WHERE rotated)                   AS total_rotations
        FROM request_logs
    """)
    if not row:
        return {"total_requests": 0, "total_tokens": 0, "total_rotations": 0}
    return dict(row)

async def setup_tables():
    await execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            key_value TEXT NOT NULL UNIQUE,
            key_prefix TEXT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Standby',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id SERIAL PRIMARY KEY,
            model VARCHAR(100),
            status_code INTEGER,
            key_prefix TEXT,
            rotated BOOLEAN DEFAULT FALSE,
            latency_ms INTEGER,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cached_tokens INTEGER DEFAULT 0,
            provider VARCHAR(20),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS cached_tokens INTEGER DEFAULT 0
    """)
    await execute("""
        ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS provider VARCHAR(20)
    """)
    # Backfill provider from the model prefix for rows written before the
    # column existed, so the routing stats view isn't blind to history.
    # Four providers use a model prefix that differs from their provider key
    # (dh/->dahl, mk/->marketku, at/->atomesus, wz/->weize); a raw split_part
    # would file their history under a provider that doesn't exist.
    await execute("""
        UPDATE request_logs
        SET provider = CASE split_part(model, '/', 1)
            WHEN 'dh' THEN 'dahl'
            WHEN 'mk' THEN 'marketku'
            WHEN 'at' THEN 'atomesus'
            WHEN 'wz' THEN 'weize'
            ELSE split_part(model, '/', 1)
        END
        WHERE provider IS NULL AND model LIKE '%/%'
    """)
    # Repair rows written by the first version of the backfill above, which
    # stored the bare model prefix.
    await execute("""
        UPDATE request_logs
        SET provider = CASE provider
            WHEN 'dh' THEN 'dahl'
            WHEN 'mk' THEN 'marketku'
            WHEN 'at' THEN 'atomesus'
            WHEN 'wz' THEN 'weize'
        END
        WHERE provider IN ('dh', 'mk', 'at', 'wz')
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_logs_provider_created
        ON request_logs(provider, created_at DESC)
    """)
    await execute("""
        CREATE TABLE IF NOT EXISTS server_config (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT
        )
    """)
    # ── Migration: add token columns if upgrading from older schema ──
    await execute("""
        ALTER TABLE request_logs
        ADD COLUMN IF NOT EXISTS input_tokens INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0
    """)
    # ── Migration: add provider column to api_keys ──
    await execute("""
        ALTER TABLE api_keys
        ADD COLUMN IF NOT EXISTS provider VARCHAR(20) DEFAULT 'kc'
    """)
    await execute("""
        CREATE TABLE IF NOT EXISTS qc_model_exhaustions (
            key_value TEXT NOT NULL REFERENCES api_keys(key_value) ON DELETE CASCADE,
            model TEXT NOT NULL,
            exhausted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            PRIMARY KEY (key_value, model)
        )
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_qc_model_exhaustions_model
        ON qc_model_exhaustions(model)
    """)

    # Provider column was added in a prior migration; new keys get provider set at insert time.
    # Add index for performance on pagination/sorting
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_logs_created_at ON request_logs(created_at DESC)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_logs_key_prefix ON request_logs(key_prefix)
    """)
    # ── Playground Tables ──
    await execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            session_id INTEGER REFERENCES chat_sessions(id) ON DELETE CASCADE,
            role VARCHAR(50) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    # ── Conversation Memory Extensions ──
    await execute("""
        ALTER TABLE chat_sessions
        ADD COLUMN IF NOT EXISTS project_identifier VARCHAR(255),
        ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(64),
        ADD COLUMN IF NOT EXISTS last_model VARCHAR(100)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_identifier
        ON chat_sessions(project_identifier)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_api_key
        ON chat_sessions(api_key_hash)
    """)
    # Distinguish real router traffic (auto-created by get_or_create_session,
    # always carries a project_identifier) from sessions the admin actually
    # started in the Playground UI, so the two never share one list again.
    await execute("""
        ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS source VARCHAR(20)
    """)
    await execute("""
        UPDATE chat_sessions
        SET source = CASE WHEN project_identifier IS NULL THEN 'playground' ELSE 'api' END
        WHERE source IS NULL
    """)
    await execute("""
        ALTER TABLE chat_sessions ALTER COLUMN source SET DEFAULT 'api'
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_source
        ON chat_sessions(source)
    """)

    # ── Brain Tables ──
    await execute("""
        CREATE TABLE IF NOT EXISTS brain_conversations (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            api_key_hash VARCHAR(64) NOT NULL,
            message_id INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
            role VARCHAR(50) NOT NULL,
            content TEXT NOT NULL,
            embedding JSONB,
            model VARCHAR(100),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'brain_conversations'
                  AND column_name = 'embedding'
                  AND data_type = 'bytea'
            ) THEN
                ALTER TABLE brain_conversations
                ALTER COLUMN embedding TYPE JSONB
                USING CASE
                    WHEN embedding IS NULL THEN NULL
                    ELSE convert_from(embedding, 'UTF8')::JSONB
                END;
            END IF;
        END $$
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_conversations_session
        ON brain_conversations(session_id)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_conversations_api_key
        ON brain_conversations(api_key_hash)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_conversations_created
        ON brain_conversations(created_at DESC)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_conversations_session_created
        ON brain_conversations(session_id, created_at)
    """)
    await execute("""
        DROP INDEX IF EXISTS idx_brain_conversations_embedding
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS brain_decisions (
            id SERIAL PRIMARY KEY,
            api_key_hash VARCHAR(64) NOT NULL,
            session_id INTEGER REFERENCES chat_sessions(id) ON DELETE CASCADE,
            decision_type VARCHAR(100),
            title TEXT NOT NULL,
            description TEXT,
            context TEXT,
            outcome TEXT,
            model_ref VARCHAR(150),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        ALTER TABLE brain_decisions ADD COLUMN IF NOT EXISTS model_ref VARCHAR(150)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_decisions_api_key
        ON brain_decisions(api_key_hash)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_decisions_model_feedback
        ON brain_decisions(api_key_hash, decision_type, outcome)
        WHERE decision_type = 'model_feedback'
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_decisions_session
        ON brain_decisions(session_id)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_decisions_created
        ON brain_decisions(created_at DESC)
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS brain_facts (
            id SERIAL PRIMARY KEY,
            api_key_hash VARCHAR(64) NOT NULL,
            session_id INTEGER REFERENCES chat_sessions(id) ON DELETE CASCADE,
            category VARCHAR(100),
            fact TEXT NOT NULL,
            source TEXT,
            confidence FLOAT DEFAULT 1.0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_facts_api_key
        ON brain_facts(api_key_hash)
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_facts_category
        ON brain_facts(category)
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS brain_profiles (
            id SERIAL PRIMARY KEY,
            api_key_hash VARCHAR(64) NOT NULL UNIQUE,
            profile_data JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_brain_profiles_api_key
        ON brain_profiles(api_key_hash)
    """)

    # Older get-then-insert session creation could race under concurrent first
    # messages. Merge any duplicates before enforcing the invariant in SQL.
    await execute("""
        DO $$
        DECLARE duplicate RECORD;
        BEGIN
            FOR duplicate IN
                SELECT id, MIN(id) OVER (
                    PARTITION BY project_identifier, api_key_hash
                ) AS keeper_id
                FROM chat_sessions
                WHERE project_identifier IS NOT NULL
            LOOP
                IF duplicate.id <> duplicate.keeper_id THEN
                    UPDATE chat_messages SET session_id = duplicate.keeper_id
                    WHERE session_id = duplicate.id;
                    UPDATE brain_conversations SET session_id = duplicate.keeper_id
                    WHERE session_id = duplicate.id;
                    UPDATE brain_decisions SET session_id = duplicate.keeper_id
                    WHERE session_id = duplicate.id;
                    UPDATE brain_facts SET session_id = duplicate.keeper_id
                    WHERE session_id = duplicate.id;
                    DELETE FROM chat_sessions WHERE id = duplicate.id;
                END IF;
            END LOOP;
        END $$
    """)
    await execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_unique_identifier
        ON chat_sessions(project_identifier, api_key_hash)
        WHERE project_identifier IS NOT NULL
    """)

    # ── Router API Keys ──
    await execute("""
        CREATE TABLE IF NOT EXISTS router_api_keys (
            id SERIAL PRIMARY KEY,
            key_value TEXT NOT NULL UNIQUE,
            key_name TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_used_at TIMESTAMP WITH TIME ZONE,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)
    await execute("""
        CREATE INDEX IF NOT EXISTS idx_router_api_keys_value
        ON router_api_keys(key_value) WHERE is_active = TRUE
    """)
    # Per-key limits. NULL expires_at / 0 token_quota both mean "unlimited",
    # so existing keys keep working untouched after this migration.
    await execute("ALTER TABLE router_api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE")
    await execute("ALTER TABLE router_api_keys ADD COLUMN IF NOT EXISTS token_quota BIGINT DEFAULT 0")
    await execute("ALTER TABLE router_api_keys ADD COLUMN IF NOT EXISTS tokens_used BIGINT DEFAULT 0")
    # Comma-separated allowlist of prefixed model ids; empty = every model.
    await execute("ALTER TABLE router_api_keys ADD COLUMN IF NOT EXISTS allowed_models TEXT NOT NULL DEFAULT ''")
    # JSON object of {model_id: extra system prompt}. Applied only to requests
    # authenticated with this key -- the same model called with any other key
    # (or the router password) is untouched.
    await execute("ALTER TABLE router_api_keys ADD COLUMN IF NOT EXISTS model_prompts TEXT NOT NULL DEFAULT '{}'")
    # JSON object of {real model id: name shown to this key}. Renames the model
    # in /v1/models and in responses, and lets requests address it by the new
    # name -- again only for this key.
    await execute("ALTER TABLE router_api_keys ADD COLUMN IF NOT EXISTS model_aliases TEXT NOT NULL DEFAULT '{}'")

    # ── Custom (admin-added) providers ──
    await execute("""
        CREATE TABLE IF NOT EXISTS custom_providers (
            id SERIAL PRIMARY KEY,
            prefix VARCHAR(20) UNIQUE NOT NULL,
            name VARCHAR(100) NOT NULL,
            base_url TEXT NOT NULL,
            api_format VARCHAR(20) NOT NULL DEFAULT 'openai',
            models TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await execute("""
        ALTER TABLE custom_providers ADD COLUMN IF NOT EXISTS models TEXT NOT NULL DEFAULT ''
    """)

    # Built-in providers the admin has chosen to remove. Routing checks this
    # before dispatching; the provider's Python code path stays in place
    # (removing it would require a deploy), it's just gated off. Re-adding a
    # key for the provider clears the disable.
    await execute("""
        CREATE TABLE IF NOT EXISTS disabled_providers (
            prefix VARCHAR(20) PRIMARY KEY,
            disabled_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)


# ── Chat Session Helpers ──
# These back the Playground UI only. Real router traffic sessions (created by
# get_or_create_session for /v1/messages continuity) share the same table but
# are tagged source='api' and deliberately excluded here.
async def get_chat_sessions():
    return await fetch("SELECT * FROM chat_sessions WHERE source = 'playground' ORDER BY updated_at DESC")

async def get_chat_session(session_id: int):
    return await fetchrow("SELECT * FROM chat_sessions WHERE id = $1 AND source = 'playground'", session_id)

async def create_chat_session(name: str):
    return await fetchrow(
        "INSERT INTO chat_sessions (name, source) VALUES ($1, 'playground') RETURNING *", name
    )

async def update_chat_session(session_id: int, name: str):
    return await fetchrow(
        "UPDATE chat_sessions SET name = $1, updated_at = NOW() WHERE id = $2 AND source = 'playground' RETURNING *",
        name, session_id
    )

async def delete_chat_session(session_id: int):
    await execute("DELETE FROM chat_sessions WHERE id = $1 AND source = 'playground'", session_id)

async def get_chat_messages(session_id: int):
    return await fetch("""
        SELECT m.* FROM chat_messages m
        JOIN chat_sessions s ON s.id = m.session_id
        WHERE m.session_id = $1 AND s.source = 'playground'
        ORDER BY m.id ASC
    """, session_id)

async def save_chat_message(session_id: int, role: str, content: str):
    await execute("UPDATE chat_sessions SET updated_at = NOW() WHERE id = $1", session_id)
    return await fetchrow("INSERT INTO chat_messages (session_id, role, content) VALUES ($1, $2, $3) RETURNING *", session_id, role, content)

# ── Conversation Memory Helpers ──
async def get_or_create_session(identifier: str, api_key_hash: str, model: str = None):
    """Atomically get or create a stable API session in one round-trip."""
    name = f"Session {identifier[:8]}"
    session = await fetchrow(
        """INSERT INTO chat_sessions
               (name, project_identifier, api_key_hash, last_model)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (project_identifier, api_key_hash)
               WHERE project_identifier IS NOT NULL
           DO UPDATE SET
               updated_at = NOW(),
               last_model = COALESCE(EXCLUDED.last_model, chat_sessions.last_model)
           RETURNING id""",
        name, identifier, api_key_hash, model
    )
    return session["id"]

async def load_session_history(session_id: int, limit: int = 20):
    """Load N most recent messages from a session in chronological order."""
    messages = await fetch(
        "SELECT role, content FROM chat_messages WHERE session_id = $1 ORDER BY id DESC LIMIT $2",
        session_id, limit
    )
    # Reverse to get chronological order (oldest first)
    return list(reversed(messages))

async def append_to_session(session_id: int, role: str, content: str):
    """Append a message to session history and return its database row."""
    await execute("UPDATE chat_sessions SET updated_at = NOW() WHERE id = $1", session_id)
    return await fetchrow(
        "INSERT INTO chat_messages (session_id, role, content) VALUES ($1, $2, $3) RETURNING *",
        session_id, role, content
    )

async def cleanup_old_sessions(retention_days: int = 30):
    """Delete sessions older than retention_days."""
    retention_days = max(1, int(retention_days))
    await execute(
        "DELETE FROM chat_sessions WHERE updated_at < NOW() - make_interval(days => $1)",
        retention_days,
    )

# ── Router API Key Helpers ──
async def create_router_api_key(key_name: str, expires_at=None, token_quota: int = 0, allowed_models: str = "", model_prompts: str = "{}", model_aliases: str = "{}"):
    """Generate a new router API key, optionally scoped by expiry/quota/models."""
    import secrets
    key_value = f"rtr_{secrets.token_urlsafe(32)}"
    return await fetchrow(
        """INSERT INTO router_api_keys (key_value, key_name, expires_at, token_quota, allowed_models, model_prompts, model_aliases)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
        key_value, key_name, expires_at, token_quota, allowed_models, model_prompts, model_aliases
    )

async def get_router_api_keys():
    """Get all router API keys with their limits and usage.

    Includes key_value: the dashboard's copy button needs it. Nothing here
    is more sensitive than the DB row itself -- the value was already stored
    in plaintext, this just lets the owner see it again after the
    show-it-once creation modal closes.
    """
    return await fetch(
        """SELECT id, key_name, key_value, created_at, last_used_at, is_active,
                  expires_at, token_quota, tokens_used, allowed_models, model_prompts, model_aliases
           FROM router_api_keys ORDER BY created_at DESC"""
    )

async def verify_router_api_key(key_value: str):
    """
    Check a router key and return its row, or None if it can't be used.

    Returns the row (rather than a bool) so callers can enforce the model
    allowlist and attribute token usage back to this key.
    """
    key = await fetchrow(
        """UPDATE router_api_keys
           SET last_used_at = NOW()
           WHERE key_value = $1
             AND is_active = TRUE
             AND (expires_at IS NULL OR expires_at > NOW())
             AND (token_quota = 0 OR tokens_used < token_quota)
           RETURNING id, token_quota, tokens_used, allowed_models,
                     model_prompts, model_aliases, expires_at""",
        key_value
    )
    if not key:
        return None
    return dict(key)

async def update_router_api_key(key_id: int, key_name: str, expires_at, token_quota: int,
                                allowed_models: str, model_prompts: str, model_aliases: str,
                                keep_expiry: bool = False):
    """
    Update a key's settings in place. The secret is never touched, so anyone
    already using the key keeps working with the new rules applied.
    """
    if keep_expiry:
        return await fetchrow(
            """UPDATE router_api_keys
               SET key_name = $2, token_quota = $3, allowed_models = $4,
                   model_prompts = $5, model_aliases = $6
               WHERE id = $1 RETURNING *""",
            key_id, key_name, token_quota, allowed_models, model_prompts, model_aliases
        )
    return await fetchrow(
        """UPDATE router_api_keys
           SET key_name = $2, expires_at = $3, token_quota = $4, allowed_models = $5,
               model_prompts = $6, model_aliases = $7
           WHERE id = $1 RETURNING *""",
        key_id, key_name, expires_at, token_quota, allowed_models, model_prompts, model_aliases
    )

async def reset_router_key_usage(key_id: int):
    """Put a key's consumed tokens back to zero (e.g. after raising its quota)."""
    return await fetchrow(
        "UPDATE router_api_keys SET tokens_used = 0 WHERE id = $1 RETURNING *", key_id
    )

async def add_router_key_token_usage(key_id: int, tokens: int):
    """Bill tokens against a router key's quota."""
    if not key_id or tokens <= 0:
        return
    await execute(
        "UPDATE router_api_keys SET tokens_used = tokens_used + $1 WHERE id = $2",
        int(tokens), key_id
    )

async def delete_router_api_key(key_id: int):
    """Delete a router API key."""
    await execute("DELETE FROM router_api_keys WHERE id = $1", key_id)


# ── Custom providers ──
async def get_custom_providers():
    return await fetch("SELECT * FROM custom_providers ORDER BY created_at ASC")

async def insert_custom_provider(prefix: str, name: str, base_url: str, api_format: str):
    return await fetchrow(
        "INSERT INTO custom_providers (prefix, name, base_url, api_format) VALUES ($1, $2, $3, $4) RETURNING *",
        prefix, name, base_url, api_format
    )

async def update_custom_provider_models(prefix: str, models_csv: str):
    await execute("UPDATE custom_providers SET models = $1 WHERE prefix = $2", models_csv, prefix)

async def delete_custom_provider(prefix: str):
    await execute("DELETE FROM custom_providers WHERE prefix = $1", prefix)

async def get_disabled_providers():
    rows = await fetch("SELECT prefix FROM disabled_providers")
    return {r["prefix"] for r in rows}

async def disable_provider(prefix: str):
    await execute(
        "INSERT INTO disabled_providers (prefix) VALUES ($1) ON CONFLICT (prefix) DO NOTHING",
        prefix
    )

async def enable_provider(prefix: str):
    await execute("DELETE FROM disabled_providers WHERE prefix = $1", prefix)
