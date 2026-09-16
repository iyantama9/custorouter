# API reference

The default local base URL is `http://localhost:4000`. Deployment paths can sit behind a TLS reverse proxy without changing API prefixes.

## Authentication

Inference routes accept one of:

```http
Authorization: Bearer <router-client-key>
```

```http
x-api-key: <router-client-key>
```

Admin and playground management routes use the authenticated dashboard session cookie. Direct Brain routes read the same headers but currently compare them with the configured global router password when that password is present; they do not call the managed client-key validator. Route behavior remains authoritative because administrative endpoints can evolve faster than this document.

## Compatible inference

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/models` | List models visible to the authenticated client |
| `GET` | `/models` | Model-list compatibility alias |
| `GET` | `/v1/v1/models` | Compatibility alias for clients with a duplicated prefix |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completion |
| `POST` | `/chat/completions` | OpenAI-compatible path alias |
| `POST` | `/v1/messages` | Anthropic-compatible messages request |
| `POST` | `/v1/v1/messages` | Compatibility alias for duplicated prefixes |
| `POST` | `/v1/messages/count_tokens` | Estimate Anthropic-style input tokens |
| `POST` | `/v1/v1/messages/count_tokens` | Token-count compatibility alias |

The model list is filtered by the authenticated client key when an allowlist exists. Streaming responses use the event format expected by the selected compatibility API. OpenAI requests routed to OpenAI-format custom providers are forwarded without protocol translation; only the provider prefix in `model` is removed and an explicitly configured per-key model prompt may be prepended.

## Request fields

The compatible chat and message surfaces accept the common fields supported by the selected protocol and upstream adapter.

| Field | Meaning |
| --- | --- |
| `model` | Runtime identifier discovered from the authenticated inventory endpoint |
| `messages` | Ordered system, user, assistant, and tool-related messages |
| `stream` | Request incremental events instead of one collected response |
| `tools` | Callable tool schemas in the selected compatibility format |
| `tool_choice` | Optional tool-selection policy |
| `temperature` | Sampling control when supported upstream |
| `max_tokens` or compatible equivalent | Bound generated output where supported |

The router does not cap or override a supplied `temperature` or `max_tokens`. OpenAI-format custom providers receive the rest of the OpenAI request unchanged, including provider-specific fields. Cross-protocol adapters translate only supported equivalents; an OpenAI request routed to an Anthropic-format custom provider must supply `max_tokens` explicitly because that API requires it. Validate cross-protocol behavior before relying on provider-specific extensions.

Custom providers can choose `Authorization: Bearer` (default) or `x-api-key` for upstream authentication. Anthropic-format providers send `anthropic-version: 2023-06-01` by default; the provider configuration can set another version or leave it empty for third-party endpoints that do not accept that header. Model discovery uses the same provider-specific authentication and version headers. Only approved Anthropic compatibility headers (`anthropic-version`, `anthropic-beta`, `anthropic-workspace-id`) can pass through from the client; the router credential itself is never forwarded as a client header.

## Streaming

Streaming preserves text deltas, reasoning fields when enabled, tool identifiers, tool names, incremental tool arguments, completion state, and usage where the upstream supplies it. A stream that disconnects before its terminal event is incomplete even if some text reached the client.

Reverse proxies must disable response buffering and allow long-lived connections. Clients should assemble tool argument fragments in event order and parse JSON only after the tool input is complete.

## Model discovery without static lists

`GET /v1/models` is the only documentation-safe source for the current inventory. The response is filtered by client policy and can change when upstreams, disabled routes, aliases, or allowlists change. This repository intentionally does not publish provider or model names in README tables.

## Brain

For built-in providers, inference Brain context is opt-in through `X-Enable-Brain: true`. Without that header, the router does not add Brain instructions or create a Brain session on the inference path. Built-in Anthropic-compatible requests can separately opt into conversation replay with `X-Enable-Memory: true`. Custom providers still bypass Brain. The router's generic system-prompt augmentation is disabled by default; a deployment can opt in with `AUGMENT_SYSTEM_PROMPT=true`. Per-key model prompts configured by the key owner still apply.

Brain retrieval now reranks a bounded mix of recent and indexed topical candidates across conversations, facts, and decisions, so relevant older records can be recalled without scanning all stored embeddings. Current-session conversation records are excluded before ranking to avoid duplicate prompt context. Extracted facts and decisions come only from user messages; code blocks and quoted lines are ignored, and repeated facts do not trigger profile rebuilds. The injected context is size-bounded and explicitly marked as untrusted memory data. This improves retrieval efficiency and reduces false memories, but pattern-based extraction and semantic matching remain heuristic rather than guaranteed correct. The initial index build runs online and may take time on a large existing database.

`GET /brain/health` is intentionally unauthenticated and reports Brain middleware counters. It does not actively probe PostgreSQL or the embedding model. Other Brain routes apply the password behavior described above and use the supplied key hash to scope data.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/brain/health` | Database and embedding health |
| `POST` | `/brain/search/conversations` | Semantic conversation search |
| `POST` | `/brain/search/decisions` | Search stored decisions |
| `POST` | `/brain/search/facts` | Search stored facts |
| `GET` | `/brain/profile` | Read the client profile |
| `GET` | `/brain/session/{session_id}/summary` | Read a session summary |
| `POST` | `/brain/decisions` | Create a decision record |
| `GET` | `/brain/decisions` | List decisions |
| `POST` | `/brain/facts` | Create a fact record |
| `GET` | `/brain/facts` | List facts |

Search, list, and summary responses are scoped to the hash of the calling client key.

Conversation search accepts a JSON body containing `query`, optional `session_id`, optional `limit`, and optional `min_similarity`. Decision and fact search accept `query` with their supported filters. Create routes require the primary decision title or fact text and accept optional session and metadata fields.

## Dashboard and administration

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Application entry page |
| `GET` | `/login` | Admin login page |
| `GET` | `/dashboard` | Operator dashboard |
| `POST` | `/api/login` | Create an admin session |
| `POST` | `/api/logout` | End an admin session |
| `GET` | `/api/status` | Runtime summary |
| `GET` | `/api/logs` | Paginated request logs |
| `GET` | `/api/providers` | Provider state |
| `GET` | `/api/models` | Administrative model inventory |
| `GET` | `/api/routing/stats` | Routing statistics |
| `GET` | `/api/brain/monitor` | Brain monitoring data |
| `GET` | `/api/brain/session/{id}/messages` | Session message inspection |
| `GET` | `/api/sse` | Live dashboard event stream |
| `GET` | `/api/router-keys` | Managed client-key inventory |

The admin router also exposes authenticated mutations for provider credentials, models, disabled providers, managed client keys, refreshes, resets, and bulk deletion. Use the dashboard or inspect the current FastAPI route schema before scripting destructive operations.

Administrative mutations can change routing, cost, data handling, or client access immediately. Export the database and use one bounded change at a time.

## Playground

Authenticated playground routes support session creation, listing, reading, updating, deletion, and chat execution. Playground calls use the same provider infrastructure and can consume upstream quota.

## Errors and quotas

Policy errors are returned before upstream dispatch. Provider errors are normalized when possible, but the body can include provider-specific diagnostic fields. A successful upstream response updates token consumption for the managed client key and writes request telemetry.

Clients should:

- use bounded retries with backoff for transient failures;
- avoid retrying authentication, expiry, quota, or allowlist errors unchanged;
- treat a dropped stream as an incomplete response;
- attach their own idempotency and trace identifiers where repeated side effects matter;
- refresh `/v1/models` rather than assuming a static inventory.

| Status | Typical meaning |
| --- | --- |
| `200` | Successful request or completed non-streaming response |
| `400` | Invalid payload, unsupported request, or upstream translation failure |
| `401` | Missing or invalid credential |
| `403` | Client policy rejects the requested identifier |
| `404` | Route or requested administrative record is unavailable |
| `429` | Client quota or upstream capacity limit |
| `500` | Router, database, translation, or upstream handling failure |
| `502` or `503` | Upstream route is unavailable where mapped by the adapter |
