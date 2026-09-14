# Brain Integration - Complete Documentation

## Overview

The LLM router now has **automatic brain functionality** built-in. Every user gets brain features automatically without any setup:

- **Semantic memory**: AI remembers past conversations using embeddings
- **Decision tracking**: Automatically extracts and tracks decisions
- **Fact extraction**: Learns facts about users and projects
- **Context injection**: Automatically injects relevant past context into new requests

## Architecture

### Core Components

```
app/brain/
├── __init__.py           # Module exports
├── embeddings.py         # Text embedding (sentence-transformers + TF-IDF fallback)
├── storage.py            # PostgreSQL database operations
├── semantic.py           # Semantic search using embeddings
├── memory.py             # Conversation memory management
├── decisions.py          # Decision and fact extraction
└── middleware.py         # Auto-injection middleware

app/routers/
└── brain.py              # REST API endpoints for brain queries
```

### Database Tables

**brain_conversations**
- Stores all conversations with embeddings
- Indexed by session_id, api_key_hash, created_at

**brain_decisions**
- Tracks decisions made during conversations
- Pattern-based extraction (no LLM calls needed)

**brain_facts**
- Stores learned facts about users/projects
- Categorized: preference, profile, project, technology, general

**brain_profiles**
- User profiles built from accumulated facts

## How It Works

### 1. Request Flow (Automatic)

When a request comes to `/v1/messages`:

```
1. Extract user message from request
2. Build brain context:
   - Search similar past conversations (semantic search)
   - Retrieve relevant facts about the user
   - Retrieve relevant past decisions
3. Inject context into system message
4. Forward request to upstream provider
5. Save conversation to brain with embeddings
6. Extract decisions and facts from response
```

### 2. Embeddings

**Primary**: the configured local sentence embedding backend
**Fallback**: Simple TF-IDF-like token hashing

Embeddings are cached in `brain_embeddings_cache.json` to avoid recomputation.

### 3. Semantic Search

Uses cosine similarity between embeddings:
- Minimum similarity threshold: 0.3 for search
- Context injection threshold: 0.4
- Can fall back to PostgreSQL pgvector or Python cosine similarity

### 4. Decision & Fact Extraction

Pattern-based extraction using regex:

**Decision patterns**:
- "I decided to..."
- "Let's use..."
- "We should..."

**Fact patterns**:
- "I prefer..."
- "My X is Y"
- "I'm using..."
- "The project uses..."

## Installation

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

New dependencies:
- `sentence-transformers==3.0.1` - Text embeddings
- `numpy==1.26.4` - Array operations

### 2. Initialize Database

The brain tables are created automatically on first run via `setup_tables()` in `app/database.py`.

Run the router once to initialize:

```bash
python -m uvicorn app.main:app --reload
```

### 3. Verify Installation

Check brain health:

```bash
curl http://localhost:8000/brain/health
```

Expected response:
```json
{"status": "ok", "brain": "enabled"}
```

## API Usage

### Automatic Brain (Default)

Brain is **always enabled** for all requests. No configuration needed.

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "<model-id>",
    "messages": [
      {"role": "user", "content": "What did we discuss about databases?"}
    ]
  }'
```

The AI will automatically:
1. Search past conversations for database discussions
2. Include relevant context in the response
3. Save this conversation for future reference

### Manual Brain Queries

#### Search Conversations

```bash
curl -X POST http://localhost:8000/brain/search/conversations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "query": "database design",
    "limit": 10,
    "min_similarity": 0.3
  }'
```

#### Search Decisions

```bash
curl -X POST http://localhost:8000/brain/search/decisions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "query": "framework choice",
    "limit": 10
  }'
```

#### Search Facts

```bash
curl -X POST http://localhost:8000/brain/search/facts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "query": "user preferences",
    "category": "preference",
    "limit": 10
  }'
```

#### Get User Profile

```bash
curl http://localhost:8000/brain/profile \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Returns:
```json
{
  "preferences": [...],
  "profile": [...],
  "projects": [...],
  "technologies": [...],
  "recent_decisions": [...]
}
```

#### List Decisions

```bash
curl http://localhost:8000/brain/decisions?limit=50 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### List Facts

```bash
curl http://localhost:8000/brain/facts?category=technology&limit=100 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Create Decision Manually

```bash
curl -X POST http://localhost:8000/brain/decisions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "title": "Use PostgreSQL for primary database",
    "description": "After evaluating options, PostgreSQL fits our needs best",
    "context": "Need ACID compliance and JSON support",
    "decision_type": "architecture"
  }'
```

#### Create Fact Manually

```bash
curl -X POST http://localhost:8000/brain/facts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "fact": "User prefers TypeScript over JavaScript",
    "category": "preference",
    "confidence": 0.9
  }'
```

## Testing

### Manual Testing

1. **Start the router**:
```bash
python -m uvicorn app.main:app --reload
```

2. **Test brain health**:
```bash
curl http://localhost:8000/brain/health
```

3. **Make a conversation request**:
```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ROUTER_PASSWORD" \
  -d '{
    "model": "<model-id>",
    "messages": [
      {"role": "user", "content": "I prefer using FastAPI for building APIs"}
    ]
  }'
```

4. **Check if fact was extracted**:
```bash
curl http://localhost:8000/brain/facts \
  -H "Authorization: Bearer YOUR_ROUTER_PASSWORD"
```

5. **Ask related question**:
```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ROUTER_PASSWORD" \
  -d '{
    "model": "<model-id>",
    "messages": [
      {"role": "user", "content": "What framework should I use for my new project?"}
    ]
  }'
```

The AI should remember your preference and suggest FastAPI.

### Automated Testing

Run the test suite:

```bash
python test_brain_integration.py
```

Tests include:
- Brain health endpoint
- Database tables existence
- Embedding generation
- Conversation save/load
- Decision extraction
- Fact extraction
- Semantic search

## Technical Details

### Context Injection Format

Brain context is injected as a system message:

```xml
<brain_context>
# Relevant Past Conversations
[Similar past conversations with similarity scores]

# Relevant Facts
[Facts about the user categorized by type]

# Relevant Decisions
[Past decisions related to the current query]
</brain_context>
```

### User Isolation

Each user is isolated by API key hash:
```python
api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
```

All brain data is stored per user using this hash.

### Session Tracking

Brain works with or without explicit session IDs:
- With X-Session-Id header: groups conversations by session
- Without header: still tracks all conversations per user

### Performance

**Embedding cache**: Computed embeddings are cached to disk
**Index optimization**: All tables indexed on key lookup columns
**Batch operations**: Embeddings can be computed in batches
**Fallback strategy**: TF-IDF fallback when sentence-transformers unavailable

## Configuration

Currently brain is always enabled. Future configuration options could be added to `.env`:

```bash
# Brain configuration (not yet implemented)
BRAIN_ENABLED=true
BRAIN_MIN_SIMILARITY=0.3
BRAIN_MAX_CONTEXT_ITEMS=3
BRAIN_EMBEDDING_MODEL=<embedding-model-id>
```

## Troubleshooting

### Issue: Embeddings are slow
**Solution**: First-time model download takes time. Subsequent calls are fast.

### Issue: ImportError for sentence-transformers
**Solution**: Install dependencies: `pip install sentence-transformers numpy`

### Issue: Brain context not appearing
**Solution**: Check minimum similarity threshold (default 0.4). Lower for more results.

### Issue: Decisions/facts not extracted
**Solution**: Patterns are conservative. Use manual API to add important items.

### Issue: Database tables not created
**Solution**: Check PostgreSQL connection and run the app once to trigger migration.

## Future Enhancements

Potential improvements:
1. **Summarization**: Compress long context into summaries
2. **Relevance scoring**: Better ranking of context items
3. **User feedback**: Learn from user corrections
4. **Multi-modal**: Support images and documents
5. **Export/import**: Backup and restore brain data
6. **Configurable patterns**: Custom extraction patterns
7. **Vector database**: Use pgvector for faster similarity search

## Integration Code Overview

### Key Files Modified

**app/routers/proxy.py** (lines 25-26, 189-221, 293-323, 617-655, 826-878):
- Import brain middleware and memory manager
- Extract user message before upstream request
- Build brain context using BrainMiddleware
- Inject context into payload
- Save conversations after streaming/non-streaming responses
- Fixed current_key scope for brain access

**app/main.py** (lines 24, 75):
- Import brain router
- Register brain router with FastAPI app

**app/database.py** (lines 128-229):
- Added brain_conversations table with embedding column
- Added brain_decisions table
- Added brain_facts table with category
- Added brain_profiles table with JSONB
- Created indexes for performance

**requirements.txt**:
- Added sentence-transformers==3.0.1
- Added numpy==1.26.4

## Summary

The brain integration is **complete and automatic**. Every user of the LLM router now has:
- ✓ Semantic memory across sessions
- ✓ Automatic decision tracking
- ✓ Automatic fact learning
- ✓ Context-aware responses
- ✓ No setup required
- ✓ User isolation via API key hashing
- ✓ REST API for manual queries

The brain works silently in the background, making the AI remember context and provide better responses over time.
