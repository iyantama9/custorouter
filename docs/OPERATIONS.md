# Operations

## Deployment shape

The repository is designed for a single-host Docker deployment with a FastAPI container and PostgreSQL. A reverse proxy should provide TLS, request-size limits, timeouts appropriate for streaming, and access control for operator pages.

The Compose configuration references `../copilot-api` as an optional sibling build context. It is not present in this checkout. Restore it before starting that service, or remove the service from the local Compose override.

## Configuration

Copy `.env.example` to `.env`, then replace every placeholder. Treat all environment values as deployment-specific. Key configuration areas are:

- database connection;
- admin username, bcrypt password hash, and session secret;
- global router compatibility credential;
- provider credentials and provider-specific URLs;
- cooldown and slow-response thresholds;
- fallback model order where supported.

Never paste production values into documentation, tickets, shell history, or committed Compose files.

## Start and verify

```bash
docker compose up -d --build
docker compose ps
curl --fail http://localhost:4000/brain/health
```

Then verify an authenticated model listing with a dedicated test client key. A healthy Brain endpoint confirms the web process and reports accumulated Brain middleware counters. It does not actively verify PostgreSQL, embeddings, or any upstream provider.

## Health model

Check four layers separately:

1. Container health and process availability.
2. PostgreSQL connectivity and schema readiness through an authenticated operation that reads persisted state.
3. Router authentication and policy with `/v1/models`.
4. A low-cost inference request for each provider family in service.

The dashboard provides lifetime request totals, live activity, latency, status, key availability, routing statistics, and Brain monitoring. Alerting should come from external infrastructure because the dashboard itself depends on the application being healthy.

## Logs and sensitive data

Request logs support diagnosis and usage accounting. They can also expose model names, errors, client identity hashes, usage, and prompt-related context. Limit retention, restrict database access, and redact exported evidence before sharing it.

Do not enable verbose HTTP logging that prints authorization headers or provider request headers.

## Backup

Back up PostgreSQL before upgrades and provider policy changes. A usable backup includes schema and data for client keys, provider state, logs needed for accounting, playground state if retained, and Brain memory.

Example with placeholders:

```bash
docker compose exec -T postgres pg_dump -U llm_router_user llm_router > router-backup.sql
```

Store dumps encrypted and outside the host. Test restoration into a separate database. Existing import and export scripts are operational utilities and may reflect older deployment layouts; inspect them before use.

## Upgrade

1. Export the current database.
2. Record the deployed commit and environment variable names.
3. Build the new image without replacing the running container.
4. Review schema changes and provider adapter changes.
5. Deploy during a controlled window.
6. Verify health, login, model listing, one request per provider family, streaming, and quota accounting.
7. Roll back the image and restore data only when schema compatibility requires it.

## Incident playbooks

### No models returned

Confirm client-key validity, expiry, quota, and model allowlist. Then inspect disabled providers, custom provider prefixes, and provider credentials.

### Requests fail across all providers

Check PostgreSQL, router logs, DNS and outbound connectivity, system time, and provider account status. Validate with one bounded request rather than creating a retry storm.

### One provider family fails

Inspect its available keys, limited timestamps, upstream status, model identifiers, and adapter response parsing. Temporarily disable the affected models if clients cannot tolerate repeated failures.

### High latency

Separate queueing, DNS, connection, time-to-first-token, and full-generation time. Compare the slow threshold with normal streaming behavior before lowering it. Aggressive rotation can amplify upstream load.

### Brain unhealthy

Check database connectivity, required tables, embedding model initialization, available disk and memory. Inference may remain available, but memory search and persistence can be incomplete.

### Token usage looks incorrect

Compare raw provider usage with normalized usage and the managed key counter. Streaming providers may report usage only in a terminal event. Avoid manual counter edits without preserving an audit record.

## Scaling notes

The login limiter and some runtime state are process-local. Multiple application replicas need shared coordination for rate limiting, key state, and event delivery. PostgreSQL remains shared persistence, but horizontal deployment requires explicit concurrency tests around rotation and quota updates.
