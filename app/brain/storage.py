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
        session_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Search conversations by embedding similarity.
        Note: This is a simple implementation. For production, use pgvector extension.
        """
        if session_id:
            rows = await fetch("""
                SELECT id, session_id, role, content, embedding, model, created_at
                FROM brain_conversations
                WHERE api_key_hash = $1 AND session_id = $2 AND embedding IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 100
            """, api_key_hash, session_id)
        else:
            rows = await fetch("""
                SELECT id, session_id, role, content, embedding, model, created_at
                FROM brain_conversations
                WHERE api_key_hash = $1 AND embedding IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 100
            """, api_key_hash)

        # Calculate similarity in Python (fallback)
        from app.brain.embeddings import cosine_similarity
        def _score_rows():
            results = []
            for row in rows:
                embedding = row["embedding"]
                if isinstance(embedding, str):
                    embedding = json.loads(embedding)
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
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get decisions"""
        if session_id and decision_type:
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
        """Save a fact"""
        await execute("""
            INSERT INTO brain_facts
            (api_key_hash, session_id, category, fact, source, confidence)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, api_key_hash, session_id, category, fact, source, confidence)

    @staticmethod
    async def get_facts(
        api_key_hash: str,
        session_id: Optional[int] = None,
        category: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get facts"""
        if session_id and category:
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
