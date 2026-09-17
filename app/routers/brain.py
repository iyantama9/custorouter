"""
Brain Router - API endpoints for brain queries

Provides REST API endpoints for:
- Semantic search over conversations
- Decision tracking
- Facts management
- User profile
"""

from typing import Optional
from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

from app.routers.proxy import _check_router_auth as _check_proxy_auth
from app.brain.semantic import SemanticSearch
from app.brain.memory import MemoryManager
from app.brain.decisions import DecisionTracker
from app.brain.storage import BrainStorage
from app.brain.middleware import BrainMiddleware, get_brain_stats


router = APIRouter(prefix="/brain", tags=["brain"])


async def _check_router_auth(request: Request):
    """Use the same router-key validation as the model proxy endpoints."""
    return await _check_proxy_auth(request)


def _get_api_key_hash(request: Request) -> Optional[str]:
    """Extract API key hash from request headers"""
    auth_header = request.headers.get("Authorization", "")
    x_api_key = request.headers.get("x-api-key", "")

    api_key = ""
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
    elif x_api_key:
        api_key = x_api_key

    if api_key:
        return BrainMiddleware.get_api_key_hash(api_key)
    return None


@router.get("/health")
async def brain_health():
    """Brain health check, including live success/failure counters.

    context_build/save errors are swallowed at the call site so a brain
    outage never breaks a chat request — this is the only place that outage
    becomes visible instead of just scrolling by in stdout.
    """
    stats = get_brain_stats()
    total_errors = stats["context_build_errors"] + stats["save_errors"]
    return {
        "status": "degraded" if total_errors > 0 and stats["last_error_at"] else "ok",
        "brain": "enabled",
        "stats": stats,
    }


@router.post("/search/conversations")
async def search_conversations(request: Request, body: dict = Body(...)):
    """
    Search conversations by semantic similarity.

    Body:
        query: Search query text
        session_id: Optional session to search within
        limit: Maximum results (default 10)
        min_similarity: Minimum similarity threshold (default 0.3)
    """
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    query = body.get("query")
    if not query:
        return JSONResponse(status_code=400, content={"error": "query is required"})

    session_id = body.get("session_id")
    limit = body.get("limit", 10)
    min_similarity = body.get("min_similarity", 0.3)

    results = await SemanticSearch.search(
        query=query,
        api_key_hash=api_key_hash,
        session_id=session_id,
        limit=limit,
        min_similarity=min_similarity
    )

    return JSONResponse(content={
        "query": query,
        "results": results,
        "count": len(results)
    })


@router.post("/search/decisions")
async def search_decisions(request: Request, body: dict = Body(...)):
    """
    Search decisions by semantic similarity.

    Body:
        query: Search query text
        limit: Maximum results (default 10)
    """
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    query = body.get("query")
    if not query:
        return JSONResponse(status_code=400, content={"error": "query is required"})

    limit = body.get("limit", 10)

    results = await SemanticSearch.search_decisions(
        query=query,
        api_key_hash=api_key_hash,
        limit=limit
    )

    return JSONResponse(content={
        "query": query,
        "results": results,
        "count": len(results)
    })


@router.post("/search/facts")
async def search_facts(request: Request, body: dict = Body(...)):
    """
    Search facts by semantic similarity.

    Body:
        query: Search query text
        category: Optional category filter
        limit: Maximum results (default 10)
    """
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    query = body.get("query")
    if not query:
        return JSONResponse(status_code=400, content={"error": "query is required"})

    category = body.get("category")
    limit = body.get("limit", 10)

    results = await SemanticSearch.search_facts(
        query=query,
        api_key_hash=api_key_hash,
        category=category,
        limit=limit
    )

    return JSONResponse(content={
        "query": query,
        "results": results,
        "count": len(results)
    })


@router.get("/profile")
async def get_profile(request: Request):
    """Get user profile built from brain data"""
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    profile = await DecisionTracker.get_user_profile(api_key_hash, persist=True)

    return JSONResponse(content=profile)


@router.get("/session/{session_id}/summary")
async def get_session_summary(request: Request, session_id: int):
    """Get session summary with brain data"""
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    summary = await MemoryManager.get_session_summary(
        session_id=session_id,
        api_key_hash=api_key_hash,
        max_messages=50
    )

    return JSONResponse(content=summary)


@router.post("/decisions")
async def create_decision(request: Request, body: dict = Body(...)):
    """
    Manually create a decision.

    Body:
        title: Decision title
        description: Optional description
        context: Optional context
        outcome: Optional outcome
        decision_type: Optional type
        session_id: Optional session ID
    """
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    title = body.get("title")
    if not title:
        return JSONResponse(status_code=400, content={"error": "title is required"})

    await BrainStorage.save_decision(
        api_key_hash=api_key_hash,
        title=title,
        description=body.get("description"),
        context=body.get("context"),
        outcome=body.get("outcome"),
        decision_type=body.get("decision_type"),
        session_id=body.get("session_id")
    )

    return JSONResponse(content={"status": "ok", "message": "Decision saved"})


@router.post("/facts")
async def create_fact(request: Request, body: dict = Body(...)):
    """
    Manually create a fact.

    Body:
        fact: Fact text
        category: Optional category
        source: Optional source
        confidence: Optional confidence (0-1)
        session_id: Optional session ID
    """
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    fact = body.get("fact")
    if not fact:
        return JSONResponse(status_code=400, content={"error": "fact is required"})

    await BrainStorage.save_fact(
        api_key_hash=api_key_hash,
        fact=fact,
        category=body.get("category"),
        source=body.get("source"),
        confidence=body.get("confidence", 1.0),
        session_id=body.get("session_id")
    )

    return JSONResponse(content={"status": "ok", "message": "Fact saved"})


@router.get("/decisions")
async def list_decisions(
    request: Request,
    session_id: Optional[int] = None,
    decision_type: Optional[str] = None,
    limit: int = 50
):
    """List decisions with optional filters"""
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    decisions = await BrainStorage.get_decisions(
        api_key_hash=api_key_hash,
        session_id=session_id,
        decision_type=decision_type,
        limit=limit
    )

    return JSONResponse(content={"decisions": decisions, "count": len(decisions)})


@router.get("/facts")
async def list_facts(
    request: Request,
    session_id: Optional[int] = None,
    category: Optional[str] = None,
    limit: int = 100
):
    """List facts with optional filters"""
    if not await _check_router_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    api_key_hash = _get_api_key_hash(request)
    if not api_key_hash:
        return JSONResponse(status_code=401, content={"error": "API key required"})

    facts = await BrainStorage.get_facts(
        api_key_hash=api_key_hash,
        session_id=session_id,
        category=category,
        limit=limit
    )

    return JSONResponse(content={"facts": facts, "count": len(facts)})
