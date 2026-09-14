# Security

## Scope

Iyan Router handles provider credentials, managed client keys, administrator sessions, prompts, model responses, usage records, and long-lived Brain memory. Treat the service and its database as sensitive infrastructure.

## Current controls

- Provider and router secrets can be loaded from environment configuration or protected database fields used by the application.
- Admin passwords are verified with bcrypt.
- Admin sessions use `HttpOnly`, `Secure`, `SameSite=Strict` cookies.
- Login attempts are rate limited within the application process.
- Managed client keys support expiration, quota, model allowlists, aliases, and prompts.
- Brain ownership uses a hash of the calling key.
- Dashboard mutation routes require an authenticated admin session.

## Required production hardening

1. Replace all example and fixed credentials before deployment.
2. Remove the PostgreSQL host port or bind it to a private interface. The current Compose file publishes `5432:5432`.
3. Replace the fixed Compose database password and rotate any environment that has used it.
4. Terminate TLS at a maintained reverse proxy and redirect HTTP to HTTPS.
5. Restrict dashboard access with a VPN, private network, or additional identity-aware proxy.
6. Store `.env`, backups, exports, and logs with least-privilege filesystem permissions.
7. Apply request body limits and streaming-aware timeouts at the proxy.
8. Use a shared rate limiter if more than one application process serves login traffic.
9. Set retention limits for request logs, playground data, and Brain memory.
10. Rotate provider and router keys after suspected exposure.

## Known gaps

- PostgreSQL is published on the host by the included Compose file.
- The Compose database password is fixed in source and unsuitable for production.
- Login throttling is in-memory and does not coordinate across replicas or survive restart.
- No repository-wide security test suite currently exercises all provider adapters and admin mutations.
- Startup-time schema changes do not provide the auditability and rollback guarantees of versioned migrations.
- Custom-provider Brain coverage is incomplete.
- Direct Brain routes do not validate managed client-key expiry, quota, or allowlists; they use the configured global router password check.

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

Recommended public routes are the compatible inference endpoints and the minimal health route required by infrastructure. Brain access should be exposed only to intended clients. Admin, playground, logs, SSE, provider configuration, and key management should remain on a trusted network.

## Data protection

Prompts and responses can contain personal, confidential, or regulated information. Document the purpose, retention period, deletion procedure, and authorized operators before storing them. Backups inherit the highest sensitivity of the data they contain.

## Dependency and image maintenance

Pin and review dependency upgrades, rebuild images regularly, scan the final image, and run with a non-root user where practical. Restrict container capabilities, filesystem writes, and outbound destinations to the providers actually used.

## Reporting a vulnerability

Do not open a public issue containing credentials, exploit details, private prompts, or production endpoints. Contact the repository owner privately with the affected revision, impact, reproduction steps, and suggested containment.
