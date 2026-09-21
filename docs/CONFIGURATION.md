# Configuration

## Sources of configuration

CustoRouter combines three configuration layers:

1. Environment variables establish process, database, security, memory, and built-in upstream settings.
2. PostgreSQL stores managed client keys, custom upstreams, disabled routes, and runtime controls.
3. The operator dashboard changes database-backed configuration without editing the deployment image.

Keep environment configuration small and stable. Use the dashboard for runtime inventories and client policy. Back up PostgreSQL before bulk edits.

## Process and security settings

| Setting | Purpose | Production guidance |
| --- | --- | --- |
| `PORT` | HTTP listen port | Keep aligned with Docker health and proxy targets |
| `DATABASE_URL` | PostgreSQL connection | Use a unique password and private network path |
| `ADMIN_USERNAME` | Dashboard account name | Change the default |
| `ADMIN_PASSWORD_HASH` | Preferred bcrypt admin verifier | Generate offline and protect as a secret |
| `ADMIN_PASSWORD` | Plain password compatibility input | Avoid when a hash can be supplied |
| `SSL_KEYFILE` | Optional direct TLS private key | Prefer TLS termination at a maintained reverse proxy |
| `SSL_CERTFILE` | Optional direct TLS certificate | Keep renewal outside the image lifecycle |

The application derives its dashboard session secret from configured admin values. Rotating those values invalidates existing sessions.

## Routing behavior

| Setting | Purpose |
| --- | --- |
| `SLOW_RESPONSE_THRESHOLD_MS` | Rotate eligible credentials after a configured slow response |
| `LIMIT_COOLDOWN_MINUTES` | Return limited credentials to standby after cooldown |
| `SHOW_REASONING` | Include supported reasoning content in compatible responses |
| `AUGMENT_SYSTEM_PROMPT` | Enable global behavior normalization instructions |

A threshold of zero disables the corresponding timing behavior where implemented. Tune against observed time-to-first-token and completion latency. Aggressive rotation can increase cost and upstream pressure.

## Upstream configuration

Built-in upstream families read base URLs, credential lists, and inventory identifiers from environment variables. Custom upstreams are configured in PostgreSQL through the dashboard. Documentation intentionally does not enumerate upstream names or model identifiers because live inventory changes.

For every upstream connection:

- use an HTTPS base URL;
- issue credentials only for the required service;
- assign a unique routing prefix;
- verify identifier discovery before enabling clients;
- test streaming, tool calls, usage accounting, and error normalization;
- document data residency and retention outside this repository.

## Managed client keys

A client key can carry an expiration, token quota, identifier allowlist, aliases, and per-identifier system instructions. Apply the narrowest policy that supports the client.

Aliases create a stable contract between clients and runtime routing. Review alias changes as production changes because they can alter cost, behavior, and data handling without modifying client code.

## Safe change procedure

1. Export the database and record the deployed commit.
2. Make one bounded policy or upstream change.
3. Confirm the authenticated inventory response.
4. Send one non-streaming and one streaming request with a test key.
5. Verify usage, logs, latency, and routing statistics.
6. Roll back the single change if validation fails.

Never test configuration by repeatedly sending production traffic after a failure. Diagnose authentication, network, policy, and upstream behavior separately.
