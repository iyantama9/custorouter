# Security

## Scope


## Current controls

- Provider and router secrets can be loaded from environment configuration or protected database fields used by the application.
- Admin passwords are verified with bcrypt.
- Admin sessions use random, revocable, 12-hour tokens in `HttpOnly`, `Secure`, `SameSite=Strict` cookies. Sessions expire on process restart.
- Login attempts are limited to 5 per client IP per 15 minutes and 30 total per minute within the application process.
- Managed client keys support expiration, quota, model allowlists, aliases, and prompts.
- Dashboard mutation routes require an authenticated admin session.

## Required production hardening

1. Replace all example and fixed credentials before deployment.
2. Keep PostgreSQL bound to loopback/private networking; the current Compose file binds it to `127.0.0.1:5432`.
3. Set a unique `POSTGRES_PASSWORD` in `.env`.
4. Terminate TLS at a maintained reverse proxy and redirect HTTP to HTTPS.
5. Restrict dashboard access with a VPN, private network, or additional identity-aware proxy.
6. Store `.env`, backups, exports, and logs with least-privilege filesystem permissions.
7. Apply request body limits and streaming-aware timeouts at the proxy.
8. Use a shared rate limiter if more than one application process serves login traffic.
10. Rotate provider and router keys after suspected exposure.
11. If `TRUST_PROXY_HEADERS=true`, set `TRUSTED_PROXY_IPS` to the exact address(es) of the reverse proxy as seen by the app (for Docker this is often the bridge gateway). Nginx must overwrite `X-Real-IP` with `$remote_addr`. Otherwise the login limiter groups users under the proxy IP.

## Known gaps

- Login throttling is in-memory and does not coordinate across replicas or survive restart.
- Admin sessions are in-memory and require sticky routing or a shared session store with more than one app worker.
- Dashboard scripts still load from third-party CDNs, although their bytes are pinned with Subresource Integrity. The CSP permits inline/eval scripts for the current frontend architecture; migrate to self-hosted scripts and a nonce-based CSP for stronger XSS isolation.
- No repository-wide security test suite currently exercises all provider adapters and admin mutations.
- Startup-time schema changes do not provide the auditability and rollback guarantees of versioned migrations.
- Managed client-key token quotas are checked before and billed after inference, so concurrent requests can overrun a strict quota.

These are deployment and engineering tasks, not implied guarantees. Operators must assess the exact deployed revision and network topology.

## Secret handling

Never commit:

- `.env` files;
- provider API keys;
- managed router client keys;
- admin passwords or password hashes copied from production;
- session secrets;
- database dumps or JSON exports;
- TLS private keys;
- request logs containing sensitive prompts or responses.

When a secret appears in Git history, removing the current file is insufficient. Revoke the credential, replace it, then clean history only if required by the repository owner.

## Client key design

Issue separate keys per application or trust domain. Set the narrowest model allowlist, an explicit expiration, and a quota appropriate for expected traffic. Do not share an administrator credential with inference clients.

Treat aliases and model prompts as policy. A change can alter cost, behavior, data residency, and downstream safety without changing client code.

## Network boundaries


## Data protection

Prompts and responses can contain personal, confidential, or regulated information. Document the purpose, retention period, deletion procedure, and authorized operators before storing them. Backups inherit the highest sensitivity of the data they contain.

## Dependency and image maintenance

Pin and review dependency upgrades, rebuild images regularly, scan the final image, and run with a non-root user where practical. Restrict container capabilities, filesystem writes, and outbound destinations to the providers actually used.

## Reporting a vulnerability

Do not open a public issue containing credentials, exploit details, private prompts, or production endpoints. Contact the repository owner privately with the affected revision, impact, reproduction steps, and suggested containment.
