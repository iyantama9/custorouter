"""
Embeddings Module - Text to Vector Embeddings

Provides text embedding functionality for semantic search.
Uses FastEmbed with ONNX Runtime for local CPU embeddings (no API calls).
"""

import os
import json
import hashlib
import asyncio
import threading
from collections import OrderedDict
from typing import List, Optional
import numpy as np


class EmbeddingProvider:
    """Base class for embedding providers"""

    def embed_text(self, text: str) -> List[float]:
        """Convert text to embedding vector"""
        raise NotImplementedError

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Convert multiple texts to embeddings"""
        return [self.embed_text(t) for t in texts]

    def similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between compatible non-zero vectors."""
        if len(vec1) != len(vec2):
            return 0.0
        a = np.array(vec1)
        b = np.array(vec2)
        denominator = np.linalg.norm(a) * np.linalg.norm(b)
        if denominator == 0:
            return 0.0
        return float(np.dot(a, b) / denominator)


class FastEmbedEmbedding(EmbeddingProvider):
    """FastEmbed ONNX embeddings for CPU-only inference."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        """Initialize the 384-dimensional MiniLM embedding model."""
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model_name=model_name)
        self.dimension = 384
        self._inference_lock = threading.Lock()
        print(f"[BRAIN] Loaded FastEmbed model: {model_name}")

    def embed_text(self, text: str) -> List[float]:
        """Embed a single text."""
        if not text.strip():
            return [0.0] * self.dimension
        with self._inference_lock:
            return next(self.model.embed([text])).tolist()

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Embed multiple texts in one ONNX inference stream."""
        if not texts:
            return []
        with self._inference_lock:
            return [embedding.tolist() for embedding in self.model.embed(texts)]


class SimpleEmbedding(EmbeddingProvider):
    """
    Fallback simple embedding using TF-IDF-like approach.
    Used when FastEmbed is not available.
    """

    def __init__(self):
        self.dimension = 128
        self.vocab = {}
        print("[BRAIN] Using simple TF-IDF embeddings (fallback)")

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization"""
        import re
        text = text.lower()
        tokens = re.findall(r'\w+', text)
        return tokens

    def embed_text(self, text: str) -> List[float]:
        """Create simple embedding based on token hashing"""
        if not text.strip():
            return [0.0] * self.dimension

        tokens = self._tokenize(text)
        vector = np.zeros(self.dimension)

        for token in tokens:
            # Hash token to dimension index
            hash_val = int(hashlib.md5(token.encode()).hexdigest(), 16)
            idx = hash_val % self.dimension
            vector[idx] += 1.0

        # Normalize
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm

        return vector.tolist()


class EmbeddingCache:
    """Thread-safe bounded LRU cache for embeddings."""

    def __init__(self, cache_file: str = ".brain_embedding_cache.json"):
        self.cache_file = cache_file
        self.max_entries = max(100, int(os.getenv("EMBEDDING_CACHE_MAX_ENTRIES", "2048")))
        self.cache = OrderedDict()
        self._lock = threading.RLock()
        self._load_cache()

    def _load_cache(self):
        """Load cache from disk"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                self.cache = OrderedDict(list(loaded.items())[-self.max_entries:])
                print(f"[BRAIN] Loaded {len(self.cache)} cached embeddings")
            except Exception as e:
                print(f"[BRAIN] Failed to load embedding cache: {e}")
                self.cache = OrderedDict()

    def get(self, text: str) -> Optional[List[float]]:
        """Get cached embedding"""
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            embedding = self.cache.get(text_hash)
            if embedding is None:
                # Read caches written by older releases once, then migrate the
                # entry to SHA-256 in memory.
                legacy_hash = hashlib.md5(text.encode()).hexdigest()
                embedding = self.cache.pop(legacy_hash, None)
                if embedding is not None:
                    self.cache[text_hash] = embedding
            if embedding is not None:
                self.cache.move_to_end(text_hash)
            return embedding

    def set(self, text: str, embedding: List[float]):
        """Cache embedding"""
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            self.cache[text_hash] = embedding
            self.cache.move_to_end(text_hash)
            while len(self.cache) > self.max_entries:
                self.cache.popitem(last=False)


# Global embedding provider instance
_embedding_provider: Optional[EmbeddingProvider] = None
_embedding_cache: Optional[EmbeddingCache] = None
_provider_lock = threading.Lock()


def get_embedding_provider() -> EmbeddingProvider:
    """Get or initialize the global embedding provider"""
    global _embedding_provider, _embedding_cache

    if _embedding_provider is None:
        with _provider_lock:
            if _embedding_provider is None:
                try:
                    _embedding_provider = FastEmbedEmbedding()
                except Exception as e:
                    print(f"[BRAIN] Could not load FastEmbed: {e}")
                    _embedding_provider = SimpleEmbedding()

                _embedding_cache = EmbeddingCache()

    return _embedding_provider


def embed_text(text: str, use_cache: bool = True) -> List[float]:
    """
    Embed a single text into vector space.

    Args:
        text: Text to embed
        use_cache: Whether to use embedding cache

    Returns:
        List of floats representing the embedding vector
    """
    if not text or not text.strip():
        provider = get_embedding_provider()
        return [0.0] * provider.dimension

    if use_cache and _embedding_cache:
        cached = _embedding_cache.get(text)
        if cached is not None:
            return cached

    provider = get_embedding_provider()
    embedding = provider.embed_text(text)

    if use_cache and _embedding_cache:
        _embedding_cache.set(text, embedding)

    return embedding


def embed_batch(texts: List[str], use_cache: bool = True) -> List[List[float]]:
    """
    Embed multiple texts into vector space.
    More efficient than calling embed_text multiple times.

    Args:
        texts: List of texts to embed
        use_cache: Whether to use embedding cache

    Returns:
        List of embedding vectors
    """
    if not texts:
        return []

    provider = get_embedding_provider()

    if not use_cache:
        return provider.embed_batch(texts)

    # Check cache first
    results = []
    texts_to_embed = []
    indices_to_embed = []

    for i, text in enumerate(texts):
        if not text or not text.strip():
            results.append([0.0] * provider.dimension)
            continue

        cached = _embedding_cache.get(text) if _embedding_cache else None
        if cached is not None:
            results.append(cached)
        else:
            results.append(None)
            texts_to_embed.append(text)
            indices_to_embed.append(i)

    # Embed uncached texts
    if texts_to_embed:
        new_embeddings = provider.embed_batch(texts_to_embed)
        for idx, embedding in zip(indices_to_embed, new_embeddings):
            results[idx] = embedding
            if _embedding_cache:
                _embedding_cache.set(texts[idx], embedding)

    return results


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculate cosine similarity between two vectors"""
    provider = get_embedding_provider()
    return provider.similarity(vec1, vec2)


async def embed_text_async(text: str, use_cache: bool = True) -> List[float]:
    """Run CPU-heavy ONNX inference outside the asyncio event loop."""
    return await asyncio.to_thread(embed_text, text, use_cache)


async def embed_batch_async(texts: List[str], use_cache: bool = True) -> List[List[float]]:
    """Batch embeddings in a worker thread so concurrent HTTP stays responsive."""
    return await asyncio.to_thread(embed_batch, texts, use_cache)
