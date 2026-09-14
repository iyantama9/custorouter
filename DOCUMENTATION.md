# CustoRouter documentation

This page is the entry point for maintainers, operators, and client developers.

| Document | Use it for |
| --- | --- |
| [README](README.md) | Product overview, quick start, examples, and current limitations |
| [Architecture](docs/ARCHITECTURE.md) | Runtime components, request lifecycle, provider adapters, Brain, and data boundaries |
| [API reference](docs/API_REFERENCE.md) | Compatible inference, admin, playground, and Brain routes |
| [Configuration](docs/CONFIGURATION.md) | Environment, client policies, upstream configuration, and safe changes |
| [Data model](docs/DATA_MODEL.md) | PostgreSQL ownership, operational records, and retention boundaries |
| [Operations](docs/OPERATIONS.md) | Configuration, deployment, health, backup, restore, monitoring, and incidents |
| [Security](docs/SECURITY.md) | Trust boundaries, secret handling, hardening priorities, and reporting |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Symptom-based diagnosis without uncontrolled retries |
| [Brain integration](BRAIN_INTEGRATION.md) | Existing implementation guide for persistent memory |

## Recommended reading paths

**Client developer:** README, API reference, then the authentication and streaming sections of Architecture.

**Operator:** README, Configuration, Operations, Security, then Troubleshooting.

**Maintainer:** Architecture, API reference, Data model, Operations, and the source modules referenced by each document.

## Documentation principles

- Runtime endpoints and model inventories are the source of truth for dynamic state.
- Examples use placeholders and never contain usable credentials.
- Claims describe implemented behavior found in this repository.
- Known deployment risks are stated directly.
- Provider counts, model counts, and key counts are not frozen in prose.

## Repository map

```text
app/
  main.py                 Application startup and router assembly
  routes/                 Admin, proxy, playground, and Brain routes
  services/               Provider clients, dispatch, translation, and Brain logic
  database.py             PostgreSQL schema and persistence helpers
templates/                Server-rendered operator pages
static/                   Dashboard styles and browser scripts
docs/                     Maintainer and operator documentation
docker-compose.yml        Local or single-host service topology
Dockerfile                Application image
```

When behavior changes, update the closest detailed document and the README section that helps users discover it.
