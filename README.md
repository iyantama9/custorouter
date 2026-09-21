<p align="center">
  <img src="docs/assets/custorouter-hero.svg" alt="CustoRouter request control plane" width="100%" />
</p>

<h1 align="center">CustoRouter</h1>

<p align="center">
  A self-hosted AI gateway for compatible APIs, access policy, credential rotation, and live operations.
</p>

<p align="center">
  <a href="DOCUMENTATION.md">Documentation</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="docs/API_REFERENCE.md">API</a> ·
  <a href="docs/OPERATIONS.md">Operations</a> ·
  <a href="docs/SECURITY.md">Security</a>
</p>

> [!CAUTION]
> This gateway handles paid credentials, client access keys, prompts, responses, and usage records. Never commit secrets, production exports, TLS private keys, or request logs. Review [Security](docs/SECURITY.md) before public deployment.

## Overview

CustoRouter exposes OpenAI-compatible and Anthropic-compatible request surfaces through one FastAPI service. A request is authenticated, checked against its client policy, mapped to the correct upstream connection, translated when required, and recorded for operations. Streaming text, reasoning events, and tool calls remain compatible with the calling protocol.

The live upstream inventory is dynamic. Clients discover what is available through `GET /v1/models`; this README intentionally does not publish provider or model lists that can become stale.

## Core capabilities

| Control plane | What it does |
| --- | --- |
| Compatible APIs | Accepts common chat, message, model-list, token-count, streaming, and tool-call formats |
| Client policy | Applies expiry, quota, allowlist, aliases, and system instructions per client key |
| Routing | Resolves each model identifier to a built-in or database-configured upstream |
| Resilience | Rotates limited credentials, applies cooldowns, detects slow responses, and uses bounded fallback |
| Operations | Provides live activity, request history, key state, routing statistics, configuration, and a playground |

## Architecture at a glance

```mermaid
flowchart TD
    A[Compatible client] --> B[Authentication]
    B --> C[Client policy]
    C --> D[Route resolution]
    D --> E[Protocol translation]
    E --> F[Credential selection]
    F --> G[Upstream request]
    G --> H[Normalized stream or response]
    H --> I[Usage and request journal]
    J --> K[Client response]

    G -->|limited or slow| L[Rotate or bounded fallback]
    L --> F
    C -->|rejected| M[Policy error]
```


## Request surfaces

| Surface | Primary path | Authentication |
| --- | --- | --- |
| Model discovery | `GET /v1/models` | Router client key |
| OpenAI-compatible chat | `POST /v1/chat/completions` | Router client key |
| Anthropic-compatible messages | `POST /v1/messages` | Router client key |
| Token count | `POST /v1/messages/count_tokens` | Router client key |
| Operator dashboard | `/dashboard` | Admin session |
| Live operator events | `/api/sse` | Admin session |

Compatibility aliases and all administrative endpoints are listed in the [API reference](docs/API_REFERENCE.md).

## Client access policy

Inference accepts `Authorization: Bearer <key>` or `x-api-key: <key>`. A managed key can define:

- expiration time;
- total token quota and usage;
- allowed model identifiers;
- client-specific aliases;
- system instructions for selected identifiers.

Policy is applied before an upstream credential is used. Aliases resolve before routing, so a stable client-facing name can move without exposing internal upstream details.




## Operator dashboard

The dashboard provides:

- live request activity through SSE;
- request status, latency, usage, and routing history;
- provider credential state and cooldown controls;
- custom upstream and model configuration;
- client key policy management;
- an authenticated chat playground.

The dashboard is an administrative surface. Place it behind TLS and a trusted network or identity-aware proxy.

## Quick start

### Requirements

- Docker Engine and Docker Compose
- PostgreSQL through the included Compose service or an external database
- a completed `.env` based on `.env.example`
- at least one upstream credential
- a strong admin password hash and session secret

```bash
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:4000/login` for the operator interface. Verify an authenticated model listing before sending inference traffic.

The Compose file references an optional sibling build context at `../copilot-api`. That directory is not included in this checkout. Restore the sibling service or remove it through a local Compose override before starting the complete stack.

## Example requests

OpenAI-compatible:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $ROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-id>",
    "messages": [{"role": "user", "content": "Check the gateway."}],
    "stream": false
  }'
```

Anthropic-compatible:

```bash
curl http://localhost:4000/v1/messages \
  -H "x-api-key: $ROUTER_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-id>",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "Check the gateway."}]
  }'
```

Discover valid identifiers from the authenticated model-list endpoint rather than copying a value from documentation.

## Configuration map

| Area | Source of truth |
| --- | --- |
| Process and database settings | `.env` and `app/config.py` |
| Built-in routing behavior | proxy and service modules under `app/` |
| Custom upstreams and client keys | PostgreSQL, managed through the dashboard |
| Container topology | `docker-compose.yml` |
| Image runtime and health check | `Dockerfile` |
| Dashboard interface | `templates/` and `static/` |

Detailed environment, deployment, backup, recovery, and incident procedures live in [Operations](docs/OPERATIONS.md).

## Persistence

PostgreSQL stores runtime keys, provider configuration, request telemetry, and playground sessions. Back up the database before upgrades or bulk configuration changes.

Exports and logs can contain secrets or sensitive prompt data. Store them encrypted, restrict access, and redact them before sharing.

## Production checklist

- replace every example and fixed credential;
- remove or privately bind the published PostgreSQL port;
- terminate TLS at a maintained reverse proxy;
- configure streaming-aware timeouts and request-size limits;
- establish log retention, backup, restore, and key-rotation procedures;
- test one bounded request for each active upstream route;
- monitor health outside the application itself.

The included Compose file currently publishes PostgreSQL on host port `5432` and contains a fixed database password. It must be hardened before public deployment.

## Repository map

```text
app/
  main.py                 Startup and router assembly
  database.py             PostgreSQL access and schema setup
templates/                Operator pages
static/                   Dashboard browser assets
docs/                     Architecture, API, operations, and security
docker-compose.yml        Service topology
Dockerfile                Application image
```

## Validation

```bash
python -m compileall app
```


## Documentation
Start with [DOCUMENTATION.md](DOCUMENTATION.md) for the architecture, API inventory, operations handbook, and security posture.

## Credits and license

Built with Python, FastAPI, Uvicorn, PostgreSQL, asyncpg, HTTPX, bcrypt, Jinja, and Docker. Project names and trademarks belong to their respective owners.

No public license is included. Source availability does not grant permission to copy, modify, redistribute, host, or create derivative works.
