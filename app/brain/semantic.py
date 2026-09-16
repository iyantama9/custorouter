"""
Semantic Search Module - Search conversations by meaning

Provides semantic search capabilities over conversation history.
"""

from typing import List, Dict, Any, Optional
import asyncio

from app.brain.embeddings import embed_text_async, embed_batch, cosine_similarity
from app.brain.storage import BrainStorage


class SemanticSearch:
    """Semantic search over conversation history"""

    @staticmethod
    async def search(
        query: str,
        api_key_hash: str,
        session_id: Optional[int] = None,
        limit: int = 10,
        min_similarity: float = 0.3,
        query_embedding: Optional[List[float]] = None,
        exclude_session_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search conversations by semantic similarity.

        Args:
            query: Search query text
            api_key_hash: User's API key hash
            session_id: Optional session to search within
            limit: Maximum number of results
            min_similarity: Minimum similarity threshold (0-1)

        Returns:
            List of matching conversations with similarity scores
        """
        # Embed the query
        if query_embedding is None:
            query_embedding = await embed_text_async(query)

        # Search in database
        results = await BrainStorage.search_conversations_by_embedding(
            api_key_hash=api_key_hash,
            query_embedding=query_embedding,
            limit=limit * 2,  # Get more results for filtering
            session_id=session_id,
            query_text=query,
            exclude_session_id=exclude_session_id,
        )

        # Filter by minimum similarity
        filtered = [r for r in results if r.get("similarity", 0) >= min_similarity]

        return filtered[:limit]

    @staticmethod
    async def find_related_conversations(
        message: str,
        api_key_hash: str,
        session_id: Optional[int] = None,
        limit: int = 5,
        query_embedding: Optional[List[float]] = None,
        exclude_session_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find related past conversations for a given message.
        Used for context injection.

        Args:
            message: Current message text
            api_key_hash: User's API key hash
            session_id: Optional session to search within
            limit: Maximum number of related conversations

        Returns:
            List of related conversations
        """
        return await SemanticSearch.search(
            query=message,
            api_key_hash=api_key_hash,
            session_id=session_id,
            limit=limit,
            min_similarity=0.4,  # Higher threshold for context injection
            query_embedding=query_embedding,
            exclude_session_id=exclude_session_id,
        )

    @staticmethod
    async def search_decisions(
        query: str,
        api_key_hash: str,
        limit: int = 10,
        query_embedding: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search past decisions by semantic similarity.

        Args:
            query: Search query
            api_key_hash: User's API key hash
            limit: Maximum number of results

        Returns:
            List of matching decisions
        """
        # Get all decisions
        decisions = await BrainStorage.get_decisions(
            api_key_hash=api_key_hash,
            limit=120,
            query_text=query,
            exclude_model_feedback=True,
        )

        if not decisions:
            return []

        # Embed query
        if query_embedding is None:
            query_embedding = await embed_text_async(query)

        def _score_decisions():
            texts = [f"{d.get('title', '')} {d.get('description', '')}" for d in decisions]
            embeddings = embed_batch(texts)
            return [
                {**decision, "similarity": cosine_similarity(query_embedding, embedding)}
                for decision, embedding in zip(decisions, embeddings)
            ]

        results = await asyncio.to_thread(_score_decisions)

        # Sort by similarity
        results.sort(key=lambda x: x["similarity"], reverse=True)

        # Filter by minimum similarity
        filtered = [r for r in results if r["similarity"] >= 0.3]

        return filtered[:limit]

    @staticmethod
    async def search_facts(
        query: str,
        api_key_hash: str,
        category: Optional[str] = None,
        limit: int = 10,
        query_embedding: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search facts by semantic similarity.

        Args:
            query: Search query
            api_key_hash: User's API key hash
            category: Optional category filter
            limit: Maximum number of results

        Returns:
            List of matching facts
        """
        # Get all facts
        facts = await BrainStorage.get_facts(
            api_key_hash=api_key_hash,
            category=category,
            limit=160,
            query_text=query,
        )

        if not facts:
            return []

        # Embed query
        if query_embedding is None:
            query_embedding = await embed_text_async(query)

        def _score_facts():
            embeddings = embed_batch([fact.get('fact', '') for fact in facts])
            return [
                {**fact, "similarity": cosine_similarity(query_embedding, embedding)}
                for fact, embedding in zip(facts, embeddings)
            ]

        results = await asyncio.to_thread(_score_facts)

        # Sort by similarity and confidence
        results.sort(key=lambda x: (x["similarity"] * x.get("confidence", 1.0)), reverse=True)

        # Filter by minimum similarity
        filtered = [r for r in results if r["similarity"] >= 0.3]

        return filtered[:limit]
