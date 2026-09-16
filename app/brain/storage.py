"""
Storage Module - Database operations for Brain

Handles:
- PostgreSQL tables for brain data
- Conversation embeddings storage
- Decisions and facts tracking
- Semantic search index
"""

import json
import asyncio
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.database import execute, fetch, fetchrow, setup_tables


def _serialize_row(row) -> Dict[str, Any]:
    """Convert an asyncpg record into JSON-safe response data."""
    data = dict(row)
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _lexical_query(text: str) -> str:
    """Build a safe, small OR query so long questions can match older notes."""
    stopwords = {"the", "and", "for", "this", "that", "with", "apa", "yang", "dan", "ini", "itu", "untuk", "saya", "aku", "bisa", "dari"}
    words = []
    for word in re.findall(r"[^\W_]{3,}", text.lower()[:500]):
        if word not in stopwords and word not in words:
            words.append(word)
        if len(words) == 12:
            break
    return " | ".join(words)


class BrainStorage:
    """Brain database storage operations"""

    @staticmethod
    async def init_tables():
        """Initialize tables through the application's single schema path."""
        await setup_tables()
        print("[BRAIN] Database tables initialized")

    @staticmethod
    async def save_conversation_embedding(
        session_id: int,
        api_key_hash: str,
        message_id: int,
        role: str,
        content: str,
        embedding: List[float],
        model: str = None
    ):
        """Save conversation with embedding"""
        await execute("""
            INSERT INTO brain_conversations
            (session_id, api_key_hash, message_id, role, content, embedding, model)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, session_id, api_key_hash, message_id, role, content, json.dumps(embedding), model)

    @staticmethod
    async def get_conversation_history(
        api_key_hash: str,
        session_id: Optional[int] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get conversation history with embeddings"""
        if session_id:
            rows = await fetch("""
                SELECT id, session_id, role, content, embedding, model, created_at
                FROM brain_conversations
                WHERE api_key_hash = $1 AND session_id = $2
                ORDER BY created_at DESC
                LIMIT $3
            """, api_key_hash, session_id, limit)
        else:
            rows = await fetch("""
                SELECT id, session_id, role, content, embedding, model, created_at
                FROM brain_conversations
                WHERE api_key_hash = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, api_key_hash, limit)

        return [_serialize_row(row) for row in rows]

    @staticmethod
    async def search_conversations_by_embedding(
        api_key_hash: str,
        query_embedding: List[float],
        limit: int = 10,
        session_id: Optional[int] = None,
        query_text: str = "",
        exclude_session_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Rerank a bounded mix of recent and indexed lexical candidates.

        This preserves older, topically matching messages without scanning all
        stored JSON embeddings or requiring pgvector on third-party installs.
        """
        rows = await fetch("""
            WITH recent_candidates AS (
                SELECT id FROM brain_conversations
                WHERE api_key_hash = $1 AND embedding IS NOT NULL
                  AND ($2::integer IS NULL OR session_id = $2)
                  AND ($3::integer IS NULL OR session_id <> $3)
                ORDER BY created_at DESC, id DESC LIMIT 120
            ), lexical_candidates AS (
                SELECT id FROM brain_conversations
                WHERE api_key_hash = $1 AND embedding IS NOT NULL
                  AND ($2::integer IS NULL OR session_id = $2)
                  AND ($3::integer IS NULL OR session_id <> $3)
                  AND to_tsvector('simple', content) @@ to_tsquery('simple', $4)
                ORDER BY ts_rank(to_tsvector('simple', content), to_tsquery('simple', $4)) DESC
                LIMIT 120
            ), candidate_ids AS (
                SELECT id FROM recent_candidates UNION SELECT id FROM lexical_candidates
            )
            SELECT bc.id, bc.session_id, bc.role, bc.content, bc.embedding,
                   bc.model, bc.created_at
            FROM brain_conversations bc JOIN candidate_ids c ON c.id = bc.id
        """, api_key_hash, session_id, exclude_session_id, _lexical_query(query_text))

        # Calculate similarity in Python (fallback)
        from app.brain.embeddings import cosine_similarity
        def _score_rows():
            results = []
            for row in rows:
                embedding = row["embedding"]
                if isinstance(embedding, str):
                    embedding = json.loads(embedding)
                if not isinstance(embedding, list) or len(embedding) != len(query_embedding):
                    continue
                similarity = cosine_similarity(query_embedding, embedding)
                results.append({
                    **_serialize_row(row),
                    "similarity": similarity
                })
            return results

        results = await asyncio.to_thread(_score_rows)

        # Sort by similarity
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    @staticmethod
    async def save_decision(
        api_key_hash: str,
        title: str,
        description: str = None,
        context: str = None,
        outcome: str = None,
        decision_type: str = None,
        session_id: int = None,
        model_ref: str = None
    ):
        """Save a decision"""
        await execute("""
            INSERT INTO brain_decisions
            (api_key_hash, session_id, decision_type, title, description, context, outcome, model_ref)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, api_key_hash, session_id, decision_type, title, description, context, outcome, model_ref)

    @staticmethod
    async def update_decision_outcome(decision_id: int, outcome: str):
        """Resolve a previously-logged decision with an observed outcome"""
        await execute("""
            UPDATE brain_decisions SET outcome = $1 WHERE id = $2
        """, outcome, decision_id)

    @staticmethod
    async def get_latest_unresolved_decision(
        api_key_hash: str,
        session_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Get the most recent decision in this session that has no outcome yet"""
        if session_id:
            row = await fetchrow("""
                SELECT * FROM brain_decisions
                WHERE api_key_hash = $1 AND session_id = $2 AND outcome IS NULL
                    AND decision_type != 'model_feedback'
                ORDER BY created_at DESC
                LIMIT 1
            """, api_key_hash, session_id)
        else:
            row = await fetchrow("""
                SELECT * FROM brain_decisions
                WHERE api_key_hash = $1 AND outcome IS NULL
                    AND decision_type != 'model_feedback'
                ORDER BY created_at DESC
                LIMIT 1
            """, api_key_hash)

        return _serialize_row(row) if row else None

    @staticmethod
    async def get_last_assistant_model(session_id: int) -> Optional[str]:
        """Get the model used for the most recent assistant reply in a session"""
        row = await fetchrow("""
            SELECT model FROM brain_conversations
            WHERE session_id = $1 AND role = 'assistant' AND model IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)
        return row["model"] if row else None

    @staticmethod
    async def get_avoided_models(api_key_hash: str, limit: int = 20) -> set:
        """
        Models that recently received explicit negative feedback from this user.
        Used to deprioritize (never hard-exclude) candidates in fallback routing.
        """
        rows = await fetch("""
            SELECT DISTINCT model_ref FROM (
                SELECT model_ref, created_at FROM brain_decisions
                WHERE api_key_hash = $1 AND decision_type = 'model_feedback'
                    AND outcome = 'negative' AND model_ref IS NOT NULL
                ORDER BY created_at DESC
                LIMIT $2
            ) recent
        """, api_key_hash, limit)
        return {row["model_ref"] for row in rows}

    @staticmethod
    async def get_decisions(
        api_key_hash: str,
        session_id: Optional[int] = None,
        decision_type: Optional[str] = None,
        limit: int = 50,
        query_text: Optional[str] = None,
        exclude_model_feedback: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get decisions"""
        if query_text is not None:
            rows = await fetch("""
                WITH recent_candidates AS (
                    SELECT id FROM brain_decisions
                    WHERE api_key_hash = $1
                      AND ($2::integer IS NULL OR session_id = $2)
                      AND ($3::text IS NULL OR decision_type = $3)
                      AND (NOT $4::boolean OR decision_type IS DISTINCT FROM 'model_feedback')
                    ORDER BY created_at DESC, id DESC LIMIT 60
                ), lexical_candidates AS (
                    SELECT id FROM brain_decisions
                    WHERE api_key_hash = $1
                      AND ($2::integer IS NULL OR session_id = $2)
                      AND ($3::text IS NULL OR decision_type = $3)
                      AND (NOT $4::boolean OR decision_type IS DISTINCT FROM 'model_feedback')
                      AND to_tsvector('simple', title) @@ to_tsquery('simple', $5)
                    ORDER BY ts_rank(to_tsvector('simple', title), to_tsquery('simple', $5)) DESC
                    LIMIT 60
                ), candidate_ids AS (
                    SELECT id FROM recent_candidates UNION SELECT id FROM lexical_candidates
                )
                SELECT d.* FROM brain_decisions d JOIN candidate_ids c ON c.id = d.id
                LIMIT $6
            """, api_key_hash, session_id, decision_type, exclude_model_feedback,
                _lexical_query(query_text), limit)
        elif session_id and decision_type:
            rows = await fetch("""
                SELECT * FROM brain_decisions
                WHERE api_key_hash = $1 AND session_id = $2 AND decision_type = $3
                ORDER BY created_at DESC
                LIMIT $4
            """, api_key_hash, session_id, decision_type, limit)
        elif session_id:
            rows = await fetch("""
                SELECT * FROM brain_decisions
                WHERE api_key_hash = $1 AND session_id = $2
                ORDER BY created_at DESC
                LIMIT $3
            """, api_key_hash, session_id, limit)
        elif decision_type:
            rows = await fetch("""
                SELECT * FROM brain_decisions
                WHERE api_key_hash = $1 AND decision_type = $2
                ORDER BY created_at DESC
                LIMIT $3
            """, api_key_hash, decision_type, limit)
        else:
            rows = await fetch("""
                SELECT * FROM brain_decisions
                WHERE api_key_hash = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, api_key_hash, limit)

        return [_serialize_row(row) for row in rows]

    @staticmethod
    async def save_profile(api_key_hash: str, profile_data: Dict[str, Any]):
        """Upsert the materialized user profile snapshot"""
        await execute("""
            INSERT INTO brain_profiles (api_key_hash, profile_data, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (api_key_hash)
            DO UPDATE SET profile_data = $2, updated_at = NOW()
        """, api_key_hash, json.dumps(profile_data))

    @staticmethod
    async def get_profile(api_key_hash: str) -> Optional[Dict[str, Any]]:
        """Get the last materialized user profile snapshot, if any"""
        row = await fetchrow("""
            SELECT profile_data, updated_at FROM brain_profiles
            WHERE api_key_hash = $1
        """, api_key_hash)
        if not row:
            return None
        profile = row["profile_data"]
        if isinstance(profile, str):
            profile = json.loads(profile)
        profile["_cached_at"] = row["updated_at"].isoformat()
        return profile

    @staticmethod
    async def save_fact(
        api_key_hash: str,
        fact: str,
        category: str = None,
        source: str = None,
        confidence: float = 1.0,
        session_id: int = None
    ):
        """Save a new fact once per user, avoiding profile bloat on repeats."""
        row = await fetchrow("""
            INSERT INTO brain_facts
                (api_key_hash, session_id, category, fact, source, confidence)
            SELECT $1, $2, $3, $4, $5, $6
            WHERE NOT EXISTS (
                SELECT 1 FROM brain_facts
                WHERE api_key_hash = $1 AND md5(lower(fact)) = md5(lower($4))
                  AND lower(fact) = lower($4)
            )
            RETURNING id
        """, api_key_hash, session_id, category, fact, source, confidence)
        return row is not None

    @staticmethod
    async def get_facts(
        api_key_hash: str,
        session_id: Optional[int] = None,
        category: Optional[str] = None,
        limit: int = 100,
        query_text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get facts"""
        if query_text is not None:
            rows = await fetch("""
                WITH recent_candidates AS (
                    SELECT id FROM brain_facts
                    WHERE api_key_hash = $1
                      AND ($2::integer IS NULL OR session_id = $2)
                      AND ($3::text IS NULL OR category = $3)
                    ORDER BY created_at DESC, id DESC LIMIT 80
                ), lexical_candidates AS (
                    SELECT id FROM brain_facts
                    WHERE api_key_hash = $1
                      AND ($2::integer IS NULL OR session_id = $2)
                      AND ($3::text IS NULL OR category = $3)
                      AND to_tsvector('simple', fact) @@ to_tsquery('simple', $4)
                    ORDER BY ts_rank(to_tsvector('simple', fact), to_tsquery('simple', $4)) DESC
                    LIMIT 80
                ), candidate_ids AS (
                    SELECT id FROM recent_candidates UNION SELECT id FROM lexical_candidates
                )
                SELECT f.* FROM brain_facts f JOIN candidate_ids c ON c.id = f.id
                LIMIT $5
            """, api_key_hash, session_id, category, _lexical_query(query_text), limit)
        elif session_id and category:
            rows = await fetch("""
                SELECT * FROM brain_facts
                WHERE api_key_hash = $1 AND session_id = $2 AND category = $3
                ORDER BY created_at DESC
                LIMIT $4
            """, api_key_hash, session_id, category, limit)
        elif session_id:
            rows = await fetch("""
                SELECT * FROM brain_facts
                WHERE api_key_hash = $1 AND session_id = $2
                ORDER BY created_at DESC
                LIMIT $3
            """, api_key_hash, session_id, limit)
        elif category:
            rows = await fetch("""
                SELECT * FROM brain_facts
                WHERE api_key_hash = $1 AND category = $2
                ORDER BY created_at DESC
                LIMIT $3
            """, api_key_hash, category, limit)
        else:
            rows = await fetch("""
                SELECT * FROM brain_facts
                WHERE api_key_hash = $1
                ORDER BY created_at DESC
                LIMIT $2
            """, api_key_hash, limit)

        return [_serialize_row(row) for row in rows]
